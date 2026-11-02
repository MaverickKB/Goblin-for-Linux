"""Background workers consuming ready leaves and ready sibling parents."""

from queue import Empty
from collections.abc import Callable
from dataclasses import dataclass
import sys
import threading
import time
from .backend import BackendError
from .baseline import SUMMARY_ROLE
from .provenance import envelope, node_lineage, record_source, stored_lineage

from .prompts import COMPACTION_RETRY_TEMPLATE, COMPACTION_TASK_TEMPLATE
from .tree import utf8_prefix
from .model_seat import ModelYielded


class CompactionStopped(Exception):
    pass


@dataclass(frozen=True)
class ModelLine:
    text: str
    hard_cut: bool
    calls: tuple[dict, ...]


class Compactor:
    def __init__(self, tree, view, backend, system_prompt, tools_schema, workers=4,
                 *, retries=2, context_bytes=16000,
                 advisory_review=None, concurrent_requests=1):
        if workers <= 0 or retries < 0 or context_bytes < 0 or concurrent_requests <= 0:
            raise ValueError('Invalid compactor worker, retry or context budget')
        self.tree = tree
        self.view = view
        self.backend = backend
        self.system_prompt = system_prompt
        self.tools_schema = tools_schema
        self.workers = workers
        self.concurrent_requests = concurrent_requests
        self.retries = retries
        self.context_bytes = min(context_bytes, max(0, view.low_bytes // 2))
        self.advisory_review = advisory_review
        self.threads = []
        self._stop = threading.Event()
        self._draining = threading.Event()
        self._lifecycle = threading.Lock()
        self.errors = []
        self._retry_after = {}
        self._failures = {}
        self._active_jobs = set()
        self._completed = 0
        self._last_progress = 0.0
        self.on_progress: Callable[[dict], None] | None = None
        self._progress_lock = threading.Lock()
        self.retry_seconds = 10.0

    def start(self):
        with self._lifecycle:
            for thread in self.threads:
                if thread.is_alive():
                    return
            # A crash can occur between a durable tree append and its View
            # callback. Resume from those built nodes even with empty queues.
            self.view.on_node(0, 0)
            self._stop.clear()
            self._draining.clear()
            self.threads = []
            for index in range(self.workers):
                thread = threading.Thread(target=self._worker,
                                          name=f'goblin-compactor-{index}', daemon=True)
                self.threads.append(thread)
                thread.start()

    def quiesce(self):
        """Finish admitted requests without starting another queued summary."""
        with self._lifecycle:
            self._draining.set()
            with self.tree.changed:
                self.tree.changed.notify_all()
            for thread in self.threads:
                thread.join()

    def stop(self):
        with self._lifecycle:
            self._stop.set()
            cancel = getattr(self.backend, "cancel", None)
            if cancel is not None:
                cancel()
            with self.tree.changed:
                self.tree.changed.notify_all()
            for thread in self.threads:
                thread.join()

    def _next_job(self, merge_first=False):
        queues = (self.tree.ready_leaves(), self.tree.ready_merges())
        if merge_first:
            queues = (self.tree.ready_merges(), self.tree.ready_leaves())
        now = time.monotonic()
        for queue in queues:
            for _ in range(queue.qsize()):
                try:
                    key = queue.get_nowait()
                except Empty:
                    break
                if now >= self._retry_after.get(key, 0):
                    return queue, key
                queue.put(key)
                queue.task_done()
        return None, None

    def status(self):
        state = self.view.status()
        with self.tree.changed:
            failures = []
            for key, error in self._failures.items():
                if not self.tree.is_built(*key):
                    failures.append({'level': key[0], 'index': key[1], **error})
            workers = 0
            for thread in self.threads:
                if thread.is_alive():
                    workers += 1
            state.update({'workers': workers,
                          'active': len(self._active_jobs), 'completed': self._completed,
                          'last_progress': self._last_progress, 'failures': failures})
            state['queued_leaves'] = self.tree.ready_leaves().qsize()
            state['queued_merges'] = self.tree.ready_merges().qsize()
            state['active_jobs'] = sorted(self._active_jobs)
            frames = sys._current_frames()
            worker_states = []
            for thread in self.threads:
                frame = frames.get(thread.ident)
                stack = []
                while frame is not None:
                    stack.append(frame.f_code.co_name + ':' + str(frame.f_lineno))
                    frame = frame.f_back
                worker_states.append({'name': thread.name, 'stack': stack})
            state['worker_states'] = worker_states
        return state

    def wait_for_context(self, *, before=None, cancelled=None, progress=None):
        previous = None
        while True:
            if cancelled is not None and cancelled.is_set():
                return None
            snapshot = self.view.ready_snapshot(before=before)
            if snapshot is not None:
                return snapshot
            state = self.status()
            if self._stop.is_set() or not state['workers']:
                raise RuntimeError('Conversation compaction is stopped; unfinished history is preserved')
            failure_signature = []
            for item in state['failures']:
                failure_signature.append((item['level'], item['index'], item['error']))
            signature = (state['bytes'], state['pending'], state['completed'], failure_signature)
            if progress is not None and signature != previous:
                progress(state)
                previous = signature
            with self.tree.changed:
                self.tree.changed.wait(timeout=0.1)

    def _worker(self):
        while not self._stop.is_set():
            state = self.view.status()
            with self.tree.changed:
                if self._draining.is_set():
                    return
                parallel = self.workers > 1 and self.concurrent_requests > 1
                merge_first = state['compacting'] and not parallel
                queue, key = self._next_job(merge_first=merge_first)
                if key is None or queue is None:
                    self.tree.changed.wait(timeout=.25)
                    continue
                self._active_jobs.add(key)
            try:
                self._publish_status()
                if not self.tree.is_built(*key):
                    text, metadata = self._summarize(*key)
                    if self.advisory_review is not None:
                        try:
                            review = self.advisory_review(text, self._source(*key))
                            if review is not None:
                                metadata['advisory_review'] = review
                        except Exception as error:
                            metadata['advisory_review'] = {'advisory': True, 'state': 'unavailable',
                                                          'reason': type(error).__name__}
                    if self._stop.is_set():
                        raise CompactionStopped()
                    metadata["provenance"] = node_lineage(self.tree, *key)
                    self.tree.set_node(*key, text, meta=metadata)
                else:
                    self.view.on_node(*key)
                with self.tree.changed:
                    self._retry_after.pop(key, None)
                    self._failures.pop(key, None)
                    self._completed += 1
                    self._last_progress = time.monotonic()
            except (CompactionStopped, ModelYielded):
                queue.put(key)
            except Exception as error:
                with self.tree.changed:
                    name = type(error).__name__
                    if key not in self._failures:
                        self.errors.append((key, name))
                    self._failures[key] = {'error': name,
                                           'category': getattr(error, 'category', 'compaction')}
                    if isinstance(error, BackendError):
                        self._failures[key].update(error.diagnostics())
                    self._retry_after[key] = time.monotonic() + self.retry_seconds
                    queue.put(key)
                    self.tree.changed.notify_all()
            finally:
                with self.tree.changed:
                    self._active_jobs.discard(key)
                    self.tree.changed.notify_all()
                queue.task_done()
                self._publish_status()

    def _publish_status(self):
        with self._progress_lock:
            if self.on_progress is not None:
                self.on_progress(self.status())

    def _source(self, level, index):
        if level == 0:
            message = self.tree.log.get(index)
            return f'[{message.kind}] {message.text}'
        parts = []
        for child in (index * 2, index * 2 + 1):
            text = self.tree.node(level - 1, child)
            if level == 1:
                kind = self.tree.log.get(child).kind
                if not text.startswith(f'[{kind}]'):
                    text = f'[{kind}] {text}'
            parts.append(text)
        return '\n'.join(parts)

    def _model_source(self, level, index):
        if level == 0:
            message = self.tree.log.get(index)
            return envelope(message.text, record_source(message))
        parts = []
        for child in (index * 2, index * 2 + 1):
            text = self.tree.node(level - 1, child)
            provenance = stored_lineage(self.tree, level - 1, child)
            parts.append(envelope(text, provenance))
        return '\n'.join(parts)

    def _fallback(self, source):
        return utf8_prefix(' '.join(source.split()), self.tree.node_bytes)

    def _summarize(self, level, index):
        source = self._source(level, index)
        budget = self.tree.node_bytes
        if len(source.encode('utf-8')) <= budget:
            return source, {}
        if self.backend is None:
            return self._fallback(source), {}
        ruler = utf8_prefix('1234567890' * ((budget + 9) // 10), budget)
        task = COMPACTION_TASK_TEMPLATE.format(start=index * (1 << level),
                                               count=1 << level, node_bytes=budget,
                                               ruler=ruler, source=self._model_source(level, index))
        end = index if level == 0 else (index + 1) * (1 << level)
        context = self.view.compaction_context(end, self.context_bytes)
        messages = [{'role': 'system', 'content': self.system_prompt + SUMMARY_ROLE},
                    {'role': 'user', 'content': context},
                    {'role': 'user', 'content': task}]
        result = self._model_line(messages, source, self.retries)
        text = result.text
        metadata = {'model_calls': list(result.calls), 'hard_cut': result.hard_cut}
        return text, metadata

    def _model_line(self, messages, source, retries):
        budget = self.tree.node_bytes
        shortest = None
        calls = []
        for attempt in range(retries + 1):
            if self._stop.is_set():
                raise CompactionStopped()
            try:
                # A proxy can wait for admission, so this is call time, not server-only time.
                started = time.monotonic()
                response = self.backend.chat(messages, tools=[],
                                             tool_choice='none', cancel_event=self._stop,
                                             max_tokens=budget,
                                             chat_template_kwargs={'enable_thinking': False})
                elapsed = time.monotonic() - started
                finish = response.get('finish_reason')
                if finish != 'stop' or response.get('tool_calls'):
                    raise BackendError('Provider did not complete the summary', kind='incomplete_response')
                text = ' '.join(response['content'].split())
                if not text:
                    raise ValueError('Empty summary')
            except ModelYielded:
                raise
            except Exception:
                if self._stop.is_set():
                    raise CompactionStopped()
                raise
            size = len(text.encode('utf-8'))
            calls.append({'elapsed_seconds': elapsed, 'output_bytes': size,
                          'usage': response.get('usage') or {}})
            if shortest is None or size < len(shortest.encode('utf-8')):
                shortest = text
            if size <= budget:
                return ModelLine(text, False, tuple(calls))
            if attempt < retries:
                cut_text = utf8_prefix(text, budget)
                retry = COMPACTION_RETRY_TEMPLATE.format(
                    actual_bytes=size, node_bytes=budget, cut_text=cut_text,
                    overflow=text[len(cut_text):])
                # A fresh list lets backends retain an accurate request trace.
                messages = messages + [{'role': 'assistant', 'content': text},
                                       {'role': 'user', 'content': retry}]
        return ModelLine(utf8_prefix(shortest, budget), True, tuple(calls))

    def wait_until_summarized(self, upto_i, timeout):
        if type(upto_i) is not int or upto_i < -1 or upto_i >= self.tree.log.count():
            raise ValueError('Summary wait index is outside the chat')
        deadline = time.monotonic() + max(0, timeout)
        with self.tree.changed:
            while not self.tree.prefix_built(upto_i):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return False
                self.tree.changed.wait(timeout=remaining)
        # Node persistence precedes view notification. Waiters need the view
        # updated too, so explicitly finish any currently eligible batch.
        self.view.on_node(0, upto_i)
        return True

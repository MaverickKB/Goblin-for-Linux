"""One persistent Goblin chat: prompt, tool loop, approvals, capture and gist."""

from dataclasses import asdict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import re
import time
import threading
import uuid

from .attachments import describe as describe_attachments
from .backend import BackendError
from .capture import Capture
from .compactor import Compactor
from .history_tools import register_history_tools
from .hooks import HookBus
from .log import ChatLog
from .model_seat import ModelCancellation, ModelSeat, ModelYielded, model_route_key
from .mcp_servers import CATALOG, register_mcp_server_tools, session_plugin_servers
from .prompts import SYSTEM_PROMPT_TEMPLATE
from .remote import register_remote_tools
from .skills import Shelf, register_skill_tools
from .help_docs import register_help_tool
from .baseline import instruction_baseline
from .provenance import SOURCE_GUIDANCE, envelope, source
from .tools import (ApprovalRefused, WorkingDirectory, approval_rule, build_registry,
                    clip_output, object_schema)
from .tree import SummaryTree
from .view import View

INTERRUPT_REPLY = "Interrupted by the user."
TEXT_TOOL_CALL = re.compile(r'<tool_call>\s*(?:\{\s*"name"\s*:\s*")?([A-Za-z_][\w.-]*)')
CLI_TOOL_CALL = re.compile(r'"tool_calls"\s*:\s*\[\s*\{\s*"name"\s*:\s*"([A-Za-z_][\w.-]*)"')


def same_model_route(first, second):
    """Whether two backend objects are the same live model route.

    The host caches one Backend per binding, so the inherited route returns the
    same object. Host backends also carry a binding_key tuple; comparing it
    covers a caller that keeps two objects for one binding. Without a key, only
    identity can prove a shared route.
    """
    if first is second:
        return True
    first_key = getattr(first, "binding_key", None)
    second_key = getattr(second, "binding_key", None)
    if first_key is None or second_key is None:
        return False
    return model_route_key(first_key) == model_route_key(second_key)


class ModelProxy:
    """Resolve one backend and its lease for each compaction request."""

    def __init__(self, provider, seat=None, model_name=None, seat_provider=None, client_lease=None):
        self.provider = provider
        self.seat = seat
        self.seat_provider = seat_provider
        self.client_lease = client_lease
        self.model_name = model_name
        self._active_backends = {}
        self._active_lock = threading.RLock()

    def _configured(self):
        name = self.model_name
        if callable(name):
            try:
                name = name()
            except Exception:
                name = ""
        return bool(name and str(name).strip())

    def chat(self, messages, tools=None, cancel_event=None, **options):
        if not self._configured():
            raise RuntimeError("No model configured for summarization")
        backend = self.provider()
        seat = self.seat
        if self.seat_provider is not None:
            seat = self.seat_provider(backend)
        lease = nullcontext((backend, seat))
        if self.client_lease is not None:
            lease = self.client_lease(backend, seat, cancel_event)
        with lease as (backend, seat):
            if seat is None:
                return self._chat_owned(backend, messages, tools, cancel_event, options)

            def cancel_request():
                self._cancel_backend(backend)

            with seat.use(self, cancel_request, background=True, stopped=cancel_event) as preempted:
                cancellation = ModelCancellation(cancel_event, preempted)
                try:
                    return self._chat_owned(backend, messages, tools, cancellation, options)
                except Exception:
                    if preempted.is_set():
                        raise ModelYielded("Background summary yielded to the conversation") from None
                    raise

    def _chat_owned(self, backend, messages, tools, cancel_event, options):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Summarization stopped")
        previous = getattr(backend, "cancel_event", None)
        if cancel_event is not None:
            backend.cancel_event = cancel_event
        request_id = object()
        with self._active_lock:
            self._active_backends[request_id] = backend
        try:
            response = backend.chat(messages, tools=tools, **options)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Summary cancelled before completion")
            return response
        finally:
            with self._active_lock:
                self._active_backends.pop(request_id, None)
            if cancel_event is not None:
                backend.cancel_event = previous

    @staticmethod
    def _cancel_backend(backend):
        if hasattr(backend, "cancel"):
            return backend.cancel()

    def cancel(self):
        with self._active_lock:
            active = list(self._active_backends.values())
        for backend in active:
            self._cancel_backend(backend)


class GoblinSession:
    def __init__(self, config_provider, backend_provider, root, *, capture, hookbus=None,
                 depth=0, allowed_tools=None, tool_ceiling=None, delta_sink=None,
                 approval=None, on_message=None, model_seat=None, host=None,
                 configure_tools=None, slug=None, compactor_backend_factory=None,
                 compactor_client_lease=None, inherited_context="", parent_view=None):
        self._inherited_context = inherited_context
        self._config_provider = config_provider
        self._backend_provider = backend_provider
        # None means compaction inherits the chat route. A factory is asked for
        # the live compaction backend on each request, never at startup, so a
        # missing or signed-out account cannot block the session from opening.
        self._compactor_backend_factory = compactor_backend_factory
        self._compactor_client_lease = compactor_client_lease
        self.config = config_provider()
        self.root = root
        self.host = host
        self.slug = slug or getattr(self.config, "slug", "goblin")
        self.depth = depth
        self.session_id = uuid.uuid4().hex
        self.turn = 0
        self.turn_meta = {}
        self._turn_request_id = ""
        self.last_turn_success = False
        self.last_turn_interrupted = False
        self.last_turn_silent = False
        self.rest_requested = False
        self._closed = False
        self._in_turn = threading.Event()
        self._turn_lock = threading.RLock()
        self.model_seat = model_seat or ModelSeat()
        # Inert until a distinct compaction route is actually requested; the
        # route decision itself is made per request from the live binding.
        self.compactor_seat = ModelSeat()
        self._request_timings = []
        # Each accepted steer is one durable user row plus its pending model
        # injection. The lock is reentrant because a log subscriber may accept
        # a steer while the final agent row is being appended.
        self._steers = []
        self._steer_lock = threading.RLock()
        self._steer_open = False
        self._interrupt = threading.Event()
        # Admission and interrupt share this lock. A Stop accepted before the
        # next turn resets its flags must survive that reset. Lock order is
        # _turn_lock -> _cancel_lock -> tools._cancel_lock. The is_cancelled
        # callback runs under _cancel_lock and must not take a lock that is
        # held across a call into session.interrupt.
        self._cancel_lock = threading.RLock()
        self.interrupt_reason = "user"
        self.delta_sink = delta_sink
        self.heartbeat_interval = 5.0
        self._stream_text = []
        self._stream_saved = False
        self._last_response_id = ""
        self._context_epoch = 0
        self._context_gist_bytes = 0
        self._context_records = 0
        self._context_base_messages = 0
        self._active_steers = []
        self._approve = approval
        self._on_message = on_message
        self._pending_work = []
        self._work_lock = threading.Lock()
        self._action_goal = ""
        self.hooks = hookbus or HookBus([])
        self.log = ChatLog(root / "log", secret_values=[])
        for previous in self.log.iter():
            if previous.meta.get("session_id") == self.session_id:
                self.turn = max(self.turn, previous.meta.get("turn", 0))
        self.tree = SummaryTree(root, self.log, node_bytes=self.config.view.node_bytes)
        self.view = View(root, self.tree, high_bytes=self.config.view.high_bytes,
                         low_bytes=self.config.view.low_bytes)
        self.log.subscribe(self.view.on_message)
        self.log.subscribe(self._receive_message)
        self.workspace = WorkingDirectory(self.config.workspace)
        self.capture = capture
        self.capture.install(self.hooks)
        from .agents import AgentManager
        self.agents = AgentManager(self)
        # A registry with no operator callback must report that no approval
        # path exists. A stand-in callback that always denies would claim an
        # operator decision that never happened.
        approval_callback = self._approve_once if self._approve is not None else None
        self.tools = build_registry(self.workspace, self.view, self.agents.spawn,
                                    self.config.tools.approval, approval_callback,
                                    self.config.tools.clip_chars, self.config.tools.timeout,
                                    policy=self.config.tools, agent_wait=self.agents.wait_for_reports,
                                    parent_view=parent_view)
        self.tools.register("agent_backends",
                            "List this Goblin's configured worker model and capability levels.",
                            object_schema({}),
                            lambda: {"backends": [{"name": "default", "model": self._config_provider().chat_model(),
                                                   "capabilities": ["routine", "standard", "complex"],
                                                   "cost_rank": 1}],
                                     "note": "One model serves every capability level in this install."})
        self.shelf = Shelf(self.config.home, self.workspace.path, self.config.skills)
        self.shelf.live = lambda: self._config_provider().skills
        register_skill_tools(self.tools, self.shelf, self.config.tools.clip_chars)
        register_help_tool(self.tools)
        register_remote_tools(self.tools, self.config.remotes, self.config.home,
                              self.config.tools.clip_chars)
        self._remote_tools = {name for name in ("remote_shell", "remote_read_file", "remote_write_file")
                              if any(remote.enabled for remote in self.config.remotes.values())}
        self._plugin_tools = ()
        self._plugin_servers = ()
        self._host_tools = ()
        self._plugins_dirty = False
        if depth == 0:
            servers = session_plugin_servers(self.config)
            self._plugin_tools = tuple(register_mcp_server_tools(
                self.tools, servers, self.config.tools.clip_chars))
            self._plugin_servers = tuple(servers)
        self._register_memory_tools()
        if configure_tools is not None:
            before = set(self.tools.names())
            configure_tools(self.tools)
            self._host_tools = tuple(sorted(set(self.tools.names()) - before))
        if allowed_tools is not None:
            self.tools = self.tools.restricted(allowed_tools)
        if tool_ceiling is not None:
            self.tools = self.tools.restricted(
                [name for name in self.tools.names() if name in tool_ceiling])
        self._bind_live_tools()
        self.tool_ceiling = set(self.tools.names())
        # Prompt accounting for the current turn. last_prompt_budget is the
        # per-component byte budget of the turn's first request; the anchors
        # let the next estimate follow the provider's own token count.
        self.last_prompt_budget = {}
        self._last_usage = {}
        self._last_budget_bytes = 0
        self._last_components = {}
        self.system_prompt = self._system_prompt()
        # Gist shares chat's model seat unless a distinct compaction route is
        # selected. Required compaction completes before foreground admission,
        # and unavailable requests remain pending.
        self.compactor = Compactor(self.tree, self.view,
                                   ModelProxy(self._compaction_backend,
                                              seat_provider=self._compactor_seat,
                                              client_lease=self._lease_compactor_client,
                                              model_name=self._compactor_model),
                                   self.system_prompt, self.tools.schemas(),
                                   workers=self.config.compactor.workers,
                                   concurrent_requests=self._compactor_client_count(self.config),
                                   retries=self.config.compactor.retries,
                                   context_bytes=self.config.compactor.context_bytes)
        self.tree.subscribe(lambda level, index: self._emit("compaction", {"phase": "built",
                                                                          "level": level, "index": index}))
        self.compactor.start()
        self._emit("session_start", {"session_id": self.session_id})
        self.turn = max(self.turn, self.capture.turn_number)

    def attach_host_tools(self, host):
        """Register computer-use and Autonomy tools once the host exists."""
        self.host = host
        from .session_tools import register_session_tools
        # Remove only the exact tools this host registered before. A plugin
        # server may legitimately own names such as computer_use__probe.
        host_names = set(getattr(self, "_host_tools", ()))
        for name in host_names:
            self.tools._tools.pop(name, None)
        before = set(self.tools.names())
        register_session_tools(host, self, self.tools)
        after = set(self.tools.names())
        self._host_tools = tuple(sorted(after - before))
        self.tool_ceiling = set(self.tools.names())
        self.compactor.tools_schema = self.tools.schemas()

    def _system_prompt(self):
        config = self._config_provider()
        text = SYSTEM_PROMPT_TEMPLATE.format(agent_name=config.agent_name,
                                             operator=config.operator_label(),
                                             user_instructions=self._instructions(config))
        return instruction_baseline(self.shelf, config.skills.baseline_mode) + '\n\n' + text + SOURCE_GUIDANCE

    def _instructions(self, config):
        text = config.user_instructions
        for item in config.identity_files():
            from pathlib import Path
            path = Path(item).expanduser()
            if not path.is_absolute():
                path = config.home / path
            text = (text + "\n\n" if text else "") + path.read_text(encoding="utf-8")
        return text

    def _bind_live_tools(self):
        def live_tools():
            current = self._config_provider()
            return getattr(current, "tools", None)
        self.tools.policy_of = live_tools
        self.tools.disabled_of = lambda: tuple(getattr(live_tools() or self.config.tools, "disabled", ()) or ())

        def allowed(name):
            current = live_tools() or self.config.tools
            return approval_rule(name, current.approval_classes, current.approval_rules) == "allow"
        self.tools.allowed = allowed

    def _register_memory_tools(self):
        self.tools.register("session_briefs",
                            "Reread the memory brief delivered this turn. It is not repeated automatically.",
                            object_schema({}), lambda: {"brief": self.capture.turn_brief()})
        register_history_tools(self.tools, self.log, clip_chars=self.config.tools.clip_chars)
        if hasattr(self.capture, "register_tools"):
            self.capture.register_tools(self.tools)

    def _receive_message(self, message):
        if message.kind == "work" and not message.meta.get("work_id"):
            with self._work_lock:
                self._pending_work.append(message)
        if self._on_message is not None:
            try:
                self._on_message(message)
            except Exception:
                pass

    def _row_meta(self, **fields):
        """Shared row identity: turn, session, and the current turn request id."""
        meta = {"turn": self.turn, "session_id": self.session_id,
                "provenance": source("agent_inference", session=self.session_id, turn=self.turn)}
        for key, value in fields.items():
            meta[key] = value
        if self._turn_request_id:
            meta["request_id"] = self._turn_request_id
        return meta

    def _approve_once(self, name, arguments):
        if self._approve is None:
            return False
        return bool(self._approve(name, arguments))

    def _emit(self, event, payload):
        base = {"session_id": self.session_id, "turn": self.turn, "cwd": str(self.workspace.path),
                "approval_policy": self.tools.approval}
        base.update(self.log.redact(self.turn_meta))
        base.update(self.log.redact(payload))
        results = self.hooks.emit(event, base)
        return self.log.redact(base), self.log.redact(results)

    def _reload_remotes(self):
        for name in list(getattr(self, "_remote_tools", ())):
            self.tools._tools.pop(name, None)
        register_remote_tools(self.tools, self.config.remotes, self.config.home,
                              self.config.tools.clip_chars)
        self._remote_tools = {name for name in ("remote_shell", "remote_read_file", "remote_write_file")
                              if any(remote.enabled for remote in self.config.remotes.values())}

    def set_workspace(self, path):
        """Point file tools and the checkout line at a chosen tree."""
        resolved = self.workspace.set(path or self.config.workspace)
        self.shelf.cwd = resolved
        return str(resolved)

    def apply_config(self, config):
        self.config = config
        self.workspace.set(config.workspace)
        self.shelf.cwd = self.workspace.path
        self.shelf.config = config.skills
        if config.skills.shelf:
            self.shelf.user_root = Path(config.skills.shelf).expanduser()
        else:
            self.shelf.user_root = Path(config.home) / "skills"
        if not self._in_turn.is_set():
            self.system_prompt = self._system_prompt()
            self.compactor.system_prompt = self.system_prompt
        self.compactor.workers = config.compactor.workers
        self.compactor.concurrent_requests = self._compactor_client_count(config)
        self.compactor.retries = config.compactor.retries
        if self._in_turn.is_set():
            self._plugins_dirty = True
        else:
            self._reload_plugins()
            self._reload_remotes()
        self._bind_live_tools()

    def _compaction_backend(self):
        """The backend compaction uses right now: its own route or the chat route."""
        if self._compactor_backend_factory is None:
            return self._backend_provider()
        return self._compactor_backend_factory()

    def _lease_compactor_client(self, backend, seat, stopped):
        if self._compactor_client_lease is None:
            return nullcontext((backend, seat))
        return self._compactor_client_lease(backend, seat, stopped)

    def _compactor_client_count(self, config):
        if self._compactor_client_lease is None:
            return 1
        return config.compactor.workers

    def _compactor_model(self):
        """The model name that decides whether compaction can run at all."""
        config = self._config_provider()
        return config.model_binding(compaction=True)[2]

    def _compactor_seat(self, compactor):
        """Compaction's seat, decided from the live binding rather than a model name.

        A distinct route gets its own seat, so the chat ModelSeat cannot starve
        it. Inherited or identical routing shares the chat seat, which keeps the
        existing ordering, preemption and Stop behavior exactly as it was.
        """
        if self._compactor_backend_factory is None:
            return self.model_seat
        try:
            chat = self._backend_provider()
        except Exception:
            # Compaction can be configured while chat is not; that is a
            # distinct route, not a reason to fail the summary request here.
            return self.compactor_seat
        if same_model_route(chat, compactor):
            return self.model_seat
        return self.compactor_seat

    def _reload_plugins(self):
        for name in list(getattr(self, "_plugin_tools", ())):
            self.tools._tools.pop(name, None)
        for name in getattr(self, "_plugin_servers", ()):
            try:
                CATALOG.close_named(name)
            except Exception:
                pass
        if self.depth != 0:
            self._plugin_tools = ()
            self._plugin_servers = ()
            return
        servers = session_plugin_servers(self.config)
        self._plugin_tools = tuple(register_mcp_server_tools(
            self.tools, servers, self.config.tools.clip_chars))
        self._plugin_servers = tuple(servers)
        self.tool_ceiling = set(self.tools.names())

    def steer(self, text):
        """Accept direction for the running turn.

        The raw user row is fsynced before True is returned. The pending entry
        only injects that row into the current model messages.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Steer text cannot be empty")
        with self._steer_lock:
            if not self._in_turn.is_set() or not self._steer_open:
                return False
            clean = self.log.redact(text)
            # The raw row is the durable acceptance record. It is fsynced by
            # ChatLog.append before True is returned, and it carries the
            # request identity of the turn that accepted it.
            provenance = source("direct_user", session=self.session_id, turn=self.turn)
            row = self.log.append("user", clean, self._row_meta(steer=True, provenance=provenance))
            self.capture.record_user_statement(clean, provenance, row.i)
            self._steers.append(clean)
            return True

    def interrupt(self, reason="user"):
        with self._cancel_lock:
            active = self._in_turn.is_set() or bool(self.agents.active())
            self.interrupt_reason = reason
            self._interrupt.set()
            self.tools.cancel()
        # Delegated work and a leased model can hold their own locks. Signal
        # them after the shared cancellation state is committed so no callback
        # can run while admission is waiting on it.
        self.agents.cancel(reason)
        try:
            self.model_seat.cancel(self)
        except Exception:
            pass
        return active

    def _admit_cancellation(self, is_cancelled):
        """Reset turn cancellation unless the admitted Stop generation is stale.

        is_cancelled runs under the same lock as interrupt, so an accepted
        Stop cannot slip between the generation check and the reset. Returning
        False leaves the interrupt and tool cancellation set; the caller then
        takes the normal interrupted path with no model or tool work.
        """
        with self._cancel_lock:
            stop_admitted = False
            if is_cancelled is not None:
                stop_admitted = bool(is_cancelled())
            if stop_admitted:
                self._interrupt.set()
                self.tools.cancel()
                return False
            self._interrupt.clear()
            self.tools.reset_cancel()
            return True

    def _close_steer(self):
        """Stop accepting direction for the turn that is ending."""
        with self._steer_lock:
            self._steer_open = False
            self._steers = []

    def _preserve_partial_stream(self, interrupted=False, failed=False):
        """Save streamed text that never became a committed response."""
        if self._stream_saved:
            return False
        text = ''.join(self._stream_text)
        if not text.strip():
            return False
        meta = self._row_meta(success=False, partial=True)
        if self._last_response_id:
            meta["response_id"] = self._last_response_id
        if interrupted:
            meta["interrupted"] = True
        if failed:
            meta["failed"] = True
        self.log.append("agent", text, meta)
        self._stream_saved = True
        return True

    def _interrupted(self):
        if not self._interrupt.is_set():
            return None
        self._close_steer()
        self.last_turn_interrupted = True
        self._preserve_partial_stream(interrupted=True)
        reply = INTERRUPT_REPLY
        self.log.append("agent", reply, self._row_meta(success=False, interrupted=True))
        return reply

    def _drain_steers(self, messages):
        with self._steer_lock:
            steers, self._steers = self._steers, []
        for clean in steers:
            self._active_steers.append(clean)
            # steer() already wrote and fsynced the raw user row. Draining
            # only injects that accepted direction into the current model
            # messages, so a reply or a restart cannot duplicate the row.
            content = "[Steer from the user during this turn] " + clean
            messages.append({"role": "user", "content": content})

    def _finish_without_steer(self):
        """Close steer acceptance when no steer is waiting on the final reply."""
        with self._steer_lock:
            if self._steers:
                return False
            self._steer_open = False
            return True

    def _drain_work(self, messages):
        for report in self.agents.claim_reports(before=self._context_records):
            self.capture.record_source_evidence(report["provenance"], report["record_index"])
            messages.append({"role": "user", "content": envelope("[" + report['name'] + "] " + report['report'], report["provenance"])})
        with self._work_lock:
            pending, self._pending_work = self._pending_work, []
        for message in pending:
            messages.append({"role": "user", "content": envelope(message.text, source("worker_report", record=message.i))})

    @staticmethod
    def _message_bytes(message):
        content = message.get('content')
        size = 0
        if isinstance(content, str):
            size += len(content.encode('utf-8'))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    size += len(part['text'].encode('utf-8'))
        calls = message.get('tool_calls')
        if calls:
            size += len(json.dumps(calls, ensure_ascii=False).encode('utf-8'))
        return size

    def _context_bytes(self, messages):
        size = self._context_gist_bytes
        for message in messages[self._context_base_messages:]:
            size += self._message_bytes(message)
        return size

    def settled_gist(self, before=None):
        def progress(state):
            self._emit('compaction', {'phase': 'waiting', **state})
            if self.delta_sink is not None:
                self.delta_sink('compaction', json.dumps(state))
        snapshot = self.compactor.wait_for_context(before=before, cancelled=self._interrupt,
                                                    progress=progress)
        if snapshot is None:
            return None
        gist = snapshot['gist']
        self._context_gist_bytes = len(gist.encode('utf-8'))
        self._context_records = snapshot['records']
        return gist

    def _context_messages(self, gist, schema, shown, images, system_prompt, attachment_notes=()):
        """Build the first request of a turn and record its component sizes.

        Order is stable first, volatile last: system, tools, inherited context,
        gist, turn state, message. The gist changes every turn, so it must not
        sit before the tool block; a provider prefix cache would drop the tool
        bytes every turn (the harness finding this port applies).
        """
        tools_block = self._tools_block(schema)
        turn_state = self._turn_state({s['function']['name'] for s in schema})
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': tools_block},
            {'role': 'user', 'content': gist},
            {'role': 'user', 'content': turn_state},
            {'role': 'user', 'content': shown},
        ]
        inherited_bytes = 0
        if self._inherited_context:
            context = ('Inherited parent conversation, with parent message IDs. '
                       'Use zoom/date with source="parent" for these IDs. '
                       'Pending-summary placeholders identify raw messages available through zoom; '
                       'they are not summaries. This is separate from your local conversation.\n'
                       '<parent-conversation>\n' + self._inherited_context + '\n</parent-conversation>')
            inherited_bytes = len(context.encode('utf-8'))
            messages.insert(2, {'role': 'user', 'content': envelope(context, source('historical_mixed', parent_scope=True))})
        for note in attachment_notes:
            messages.append({'role': 'user', 'content': envelope(note, source('attachment_data'))})
        for image in images:
            messages.append({'role': 'user', 'content': [
                {'type': 'text', 'text': envelope(image['caption'], source('attachment_data'))},
                {'type': 'image_url', 'image_url': {'url': image['data_url']}},
            ]})
        self._last_components = {
            'system': len(system_prompt.encode('utf-8')),
            'tools_block': len(tools_block.encode('utf-8')),
            'inherited': inherited_bytes,
            'gist': len(gist.encode('utf-8')),
            'state': len(turn_state.encode('utf-8')),
            'message': len(shown.encode('utf-8')),
        }
        self._context_base_messages = len(messages)
        return messages

    @contextmanager
    def _heartbeat(self, label):
        """Emit a live phase update while a blocking model or tool call runs.

        The turn thread owns the real work. This small daemon thread only
        reports that the engine is still inside a known phase, so a long wait
        is visibly alive instead of looking frozen.
        """
        sink = self.delta_sink
        if sink is None:
            yield
            return
        stop = threading.Event()
        started = time.monotonic()
        interval = getattr(self, "heartbeat_interval", 5.0)

        def beat():
            while not stop.wait(interval):
                elapsed = int(time.monotonic() - started)
                sink("heartbeat", "%s (%ds)" % (label, elapsed))

        thread = threading.Thread(target=beat, name="goblin-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def _chat(self, messages, schema):
        self._stream_text = []
        self._stream_saved = False
        self._last_response_id = ""
        self._prune_tool_history(messages)
        backend = self._backend_provider()
        options = {}
        _, _, _, _, _, _, level, thinking_budget = self.config.model_binding()
        if level == "off":
            options = {"chat_template_kwargs": {"enable_thinking": False}}
        else:
            options = {"reasoning_effort": level, "chat_template_kwargs": {"enable_thinking": True}}
            if thinking_budget > 0:
                options["thinking_budget"] = thinking_budget

        waiting = time.monotonic()
        if self.delta_sink is not None:
            self.delta_sink("status", "Preparing the response")
        with self.model_seat.use(self, backend.cancel, stopped=self._interrupt):
            if self._interrupt.is_set():
                raise RuntimeError("Turn interrupted before model admission")
            started = time.monotonic()
            timing = {"queue_seconds": started - waiting, "first_output_seconds": None,
                      "context_epoch": self._context_epoch,
                      "gist_bytes": self._context_gist_bytes,
                      "gist_records": self._context_records,
                      "continuation_context_bytes": self._context_bytes(messages)}

            def delta(kind, text):
                if timing["first_output_seconds"] is None and kind in ("text", "thought", "tool") and text:
                    timing["first_output_seconds"] = time.monotonic() - started
                cleaned = self.log.redact(text)
                if kind == "text":
                    self._stream_text.append(cleaned)
                if self.delta_sink is not None:
                    self.delta_sink(kind, cleaned)

            response_id = uuid.uuid4().hex
            self._last_response_id = response_id
            backend.cancel_event = self._interrupt
            backend.on_delta = delta if self.delta_sink is not None else None
            if self.delta_sink is not None:
                self.delta_sink("model_start", response_id)
            try:
                with self._heartbeat("Waiting on the model"):
                    result = backend.chat(messages, tools=schema, **options)
            finally:
                backend.on_delta = None
                timing["response_seconds"] = time.monotonic() - started
                self._request_timings.append(timing)
            if isinstance(result, dict):
                result["response_id"] = response_id
            return result

    def _attachment_content(self, refs):
        if not refs:
            return [], []
        try:
            return describe_attachments(self.root, refs)
        except (OSError, ValueError) as exc:
            return ["Attached files could not be opened (" + str(exc) + ")."], []

    def _turn_state(self, offered):
        memory = ""
        try:
            memory = self.capture.inheritance_block()
        except Exception as error:
            memory = "Memory unavailable (" + type(error).__name__ + ")."
        lines = [
            "<turn-state>",
            "Date: " + datetime.now(timezone.utc).isoformat(),
            "Workspace: " + str(self.workspace.path),
            "Turn: " + str(self.turn),
            "Goblin tool policy: " + self.tools.approval + ". A tool reported as Blocked did not run.",
            "Transcript: history_search/history_read/history_zoom read this session's own log.",
        ]
        if memory:
            lines.append(memory)
        brief = self.capture.turn_brief()
        if brief:
            lines.append(brief)
        lines.append("</turn-state>")
        return "\n".join(lines)

    def _tools_block(self, schema):
        """The readable tool copy in the prompt.

        The backend receives the authoritative schema through the API tools
        parameter, so copying the full JSON here again doubled the cost of
        every turn. compact (default) is one line per tool; full, names and
        off are available for models that need more or nothing.
        """
        mode = self.config.tools.inline_schemas
        if mode == "off" or not schema:
            return ""
        lines = ["<tools>"]
        for item in schema:
            function = item["function"]
            if mode == "full":
                payload = {"name": function["name"], "description": function.get("description", ""),
                           "parameters": function.get("parameters", {})}
                lines.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
                continue
            if mode == "names":
                lines.append(function["name"])
                continue
            description = (function.get("description") or "").strip().split("\n")[0]
            if len(description) > 96:
                description = description[:93] + "..."
            lines.append(function["name"] + (" - " + description if description else ""))
        lines.append("</tools>")
        return "\n".join(lines)

    def _route_schema(self):
        """The model-facing tool schema for this turn.

        Scoped exposure keeps the core tools in the schema and puts the rest
        behind one tool_use envelope. A deferred tool stays registered and
        executes when the model names it directly; the registry, not the
        schema, is the capability boundary.
        """
        schemas = self.tools.schemas()
        if self.config.tools.exposure != "scoped":
            return schemas
        patterns = self.config.tools.core or []
        offered = []
        deferred = []
        for item in schemas:
            name = item["function"]["name"]
            matched = False
            for pattern in patterns:
                if fnmatch.fnmatchcase(name, pattern):
                    matched = True
                    break
            if matched:
                offered.append(item)
            else:
                deferred.append(name)
        offered.append(self._tool_use_schema(deferred))
        return offered

    @staticmethod
    def _tool_use_schema(deferred):
        names = ", ".join(deferred)
        return {"type": "function", "function": {
            "name": "tool_use",
            "description": ("Invoke a registered tool that is not in this turn short list. "
                            "Set describe=true to get the tool schema, or pass the arguments "
                            "object to run it. Deferred tools: " + names),
            "parameters": {"type": "object",
                           "properties": {"name": {"type": "string"},
                                          "arguments": {"type": "object"},
                                          "describe": {"type": "boolean"}},
                           "required": ["name"]}}}

    def _unwrap_tool_use(self, call):
        """Return (inner call, error text, describe text) for one model tool call.

        describe=true returns the tool schema without running it: that is how a
        model learns the parameter names of a deferred tool. Any other tool_use
        call names one registered tool and takes the normal call path.
        """
        if call.get("name") != "tool_use":
            return call, "", ""
        arguments = call.get("arguments") or {}
        name = arguments.get("name")
        inner = arguments.get("arguments") or {}
        if not isinstance(name, str) or not name or name == "tool_use":
            return None, "tool_use needs a registered tool name other than tool_use.", ""
        if name not in set(self.tools.names()):
            return None, "Unknown tool: " + name + ". Use a name from the tool_use description.", ""
        if arguments.get("describe"):
            return None, "", self._tool_schema_text(name)
        if not isinstance(inner, dict):
            return None, "tool_use arguments must be an object.", ""
        return {"id": call.get("id"), "name": name, "arguments": inner}, "", ""

    def _tool_schema_text(self, name):
        for item in self.tools.schemas():
            if item["function"]["name"] != name:
                continue
            function = item["function"]
            return json.dumps({"name": name, "description": function.get("description", ""),
                               "parameters": function.get("parameters", {})}, ensure_ascii=False)
        return "Unknown tool: " + name

    def _envelope_result(self, call, text, success):
        """Log a tool_use envelope answer that did not run a registered tool."""
        arguments = call.get("arguments") or {}
        call_id = call.get("id") or uuid.uuid4().hex
        self.log.append("tool", json.dumps({"name": "tool_use", "arguments": arguments}),
                        self._row_meta(call_id=call_id, name="tool_use",
                                       provenance=source("agent_inference", tool="tool_use")))
        echo_meta = self._row_meta(call_id=call_id, success=success, blocked=not success,
                                   name="tool_use",
                                   provenance=source("tool_data", tool="tool_use", owner="built-in",
                                                     call_id=call_id))
        echo = self.log.append("echo", text, echo_meta)
        echo_meta["provenance"]["record"] = echo.i
        return {"id": call_id, "name": "tool_use", "tool": "tool_use", "arguments": arguments,
                "result": text, "output": text, "success": success, "blocked": not success,
                "error": None if success else text, "image": None, "images": [], "diffs": [],
                "provenance": echo_meta["provenance"], "record_index": echo.i}

    def _prune_tool_history(self, messages):
        """Trim older tool results in the model-facing history; the log keeps the original.

        The newest result stays whole because the step that asked for it is
        still reasoning about it. Everything older keeps its head and tail
        with a marker, which is what keeps a many-step turn from resending
        every earlier full result on every step.
        """
        limit = self.config.tools.history_result_chars
        if limit <= 0:
            return
        newest_seen = False
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            if not newest_seen:
                newest_seen = True
                continue
            content = message.get("content")
            if isinstance(content, str) and len(content) > limit:
                message["content"] = clip_output(content, limit)

    def _note_usage(self, response):
        """Keep the provider's own prompt count as the anchor for the next estimate."""
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        if not isinstance(prompt, int) or prompt <= 0:
            return
        self._last_usage = {"prompt_tokens": prompt}

    def _prompt_budget(self, messages, schema):
        """What this turn's first request sends, by component, in bytes.

        The token estimate anchors to the last provider prompt count over the
        last measured budget, so it follows the real tokenizer instead of a
        fixed four characters per token.
        """
        sizes: dict = dict(self._last_components)
        named = ("system", "tools_block", "inherited", "gist", "state", "message")
        counted = 0
        for name in named:
            counted += sizes.get(name, 0)
        extra = 0
        for index, message in enumerate(messages):
            if index < self._context_base_messages:
                continue
            extra += self._message_bytes(message)
        sizes["attachments"] = extra
        sizes["tools_schema"] = len(json.dumps(schema or [], ensure_ascii=False).encode("utf-8"))
        total = counted + extra + sizes["tools_schema"]
        prompt_tokens = self._last_usage.get("prompt_tokens")
        anchor = 0
        ratio = 0.0
        if isinstance(prompt_tokens, int) and prompt_tokens > 0 and self._last_budget_bytes > 0:
            anchor = prompt_tokens
            ratio = prompt_tokens / self._last_budget_bytes
        sizes["total"] = total
        sizes["estimated_tokens"] = int(total * ratio) if ratio else total // 4
        sizes["anchor_prompt_tokens"] = anchor
        sizes["anchor_ratio"] = round(ratio, 4)
        sizes["tools"] = len(schema or [])
        return sizes

    def run_turn(self, message, turn_meta=None, *, is_cancelled=None):
        """Run one turn.

        is_cancelled reports whether the host Stop generation captured when
        this turn was admitted is stale. Existing standalone callers omit it
        and keep the ordinary admission reset.
        """
        with self._turn_lock:
            if self._closed:
                raise RuntimeError("Session is closed")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("Message cannot be empty")
            if getattr(self.capture, "client", None) is None:
                raise RuntimeError("AetherMind is unavailable; memory is a required capability")
            system_prompt = self._system_prompt()
            self.system_prompt = system_prompt
            self.compactor.system_prompt = system_prompt
            self.turn_meta = dict(turn_meta or {})
            self._turn_request_id = str(self.turn_meta.get("request_id") or "")
            self._request_timings = []
            self._last_response_id = ""
            self.last_turn_interrupted = False
            self.last_turn_silent = False
            self.rest_requested = False
            self.turn += 1
            message = self.log.redact(message)
            self._action_goal = message
            gist_before = self.log.count()
            self._context_epoch = 0
            self._active_steers = []
            attached = self.turn_meta.pop("attachments", None) or []
            # Admission owns the cancel flags. Check the host generation before
            # writing the row so a preflight Stop is recorded as canceled
            # input, then keep the flags set for the interrupted return below.
            proceed = self._admit_cancellation(is_cancelled)
            user_meta = self._row_meta(**self.turn_meta)
            if attached:
                user_meta["attachments"] = attached
            if not proceed:
                user_meta["cancelled"] = True
                user_meta["cancel_reason"] = INTERRUPT_REPLY
            input_origin = 'direct_user'
            if self.depth or self.turn_meta.get('wake') or self.turn_meta.get('source') not in (None, '', 'chat', 'voice'):
                input_origin = 'agent_instruction'
            admission = 'worker' if self.depth else ('autonomy' if self.turn_meta.get('wake') else self.turn_meta.get('source') or 'chat')
            user_meta['provenance'] = source(input_origin, session=self.session_id, turn=self.turn, admission=admission)
            input_row = self.log.append("user", message, user_meta)
            self._in_turn.set()
            reply = ""
            success = False
            failure = None
            tool_records = []
            view_deferred = False
            try:
                self._emit("turn_start", {"message": message, "text": message, "wake": bool(self.turn_meta.get("wake")),
                                          "provenance": user_meta["provenance"], "record_index": input_row.i})
                if not proceed:
                    # A Stop accepted before this turn's reset is still
                    # authoritative. No model or tool work starts.
                    reply = self._interrupted()
                    return reply
                with self._steer_lock:
                    self._steers = []
                    self._steer_open = True
                schema = self._route_schema()
                notes, images = self._attachment_content(attached)
                shown = message
                if input_origin != 'direct_user':
                    shown = envelope(message, user_meta["provenance"])
                gist = self.settled_gist(before=gist_before)
                if gist is None:
                    reply = self._interrupted()
                    return reply
                self.view.defer_merges()
                view_deferred = True
                messages = self._context_messages(gist, schema, shown, images, system_prompt, notes)
                budget = self._prompt_budget(messages, schema)
                self.last_prompt_budget = budget
                self._last_budget_bytes = budget["total"]
                limit = self.config.prompt_budget.max_bytes
                if limit and budget["total"] > limit:
                    # One deterministic reduction before refusing: the newest
                    # gist lines that fit the view low watermark.
                    smaller = self.view.prompt_render(self.config.view.low_bytes)
                    if smaller != gist:
                        gist = smaller
                        self._context_gist_bytes = len(gist.encode("utf-8"))
                        messages = self._context_messages(gist, schema, shown, images, system_prompt, notes)
                        budget = self._prompt_budget(messages, schema)
                        self.last_prompt_budget = budget
                        self._last_budget_bytes = budget["total"]
                if limit and budget["total"] > limit:
                    components = ", ".join(
                        "%s=%s" % (name, budget.get(name))
                        for name in ("system", "tools_block", "inherited", "gist",
                                     "state", "message", "attachments", "tools_schema"))
                    self.log.append("note", "Prompt over budget: " + json.dumps(budget, sort_keys=True),
                                    {"prompt_budget": budget})
                    raise BackendError(
                        "Prompt is over the configured budget (%d > %d bytes; %s). "
                        "Raise advanced.prompt.max_bytes, or let compaction shrink the history, "
                        "before retrying." % (budget["total"], limit, components),
                        kind="prompt_budget")
                if (self.config.prompt_budget.budget_note_bytes
                        and budget["total"] >= self.config.prompt_budget.budget_note_bytes):
                    self.log.append("note", "Prompt budget: " + json.dumps(budget, sort_keys=True),
                                    {"prompt_budget": budget})
                while True:
                    self._drain_work(messages)
                    self._drain_steers(messages)
                    stopped = self._interrupted()
                    if stopped:
                        reply = stopped
                        return reply
                    try:
                        response = self.log.redact(self._chat(messages, schema))
                        self._note_usage(response)
                    except Exception:
                        stopped = self._interrupted()
                        if stopped:
                            reply = stopped
                            return reply
                        raise
                    stopped = self._interrupted()
                    if stopped:
                        reply = stopped
                        return reply
                    content = response.get("content") or ""
                    calls = response.get("tool_calls") or []
                    metadata = self._row_meta(tool_calls=calls, usage=response.get("usage", {}),
                                              model_timings=list(self._request_timings[-1:]))
                    response_id = response.get("response_id")
                    if response_id:
                        metadata["response_id"] = response_id
                    if response.get("reasoning"):
                        metadata["thought"] = response["reasoning"]
                    if not calls:
                        self.log.append("agent", content, metadata)
                        self._stream_saved = True
                        if not self._finish_without_steer():
                            if content:
                                messages.append({"role": "assistant", "content": content})
                            continue
                        reply = content
                        success = True
                        self.last_turn_silent = self.rest_requested or not content.strip()
                        return reply
                    if content or metadata.get("thought"):
                        self.log.append("agent", content, metadata)
                        self._stream_saved = True
                    assistant = {"role": "assistant", "content": content or None, "tool_calls": []}
                    for call in calls:
                        assistant["tool_calls"].append({
                            "id": call["id"], "type": "function",
                            "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
                        })
                    messages.append(assistant)
                    batch_contexts = []
                    batch_images = []
                    for call in calls:
                        stopped = self._interrupted()
                        if stopped:
                            reply = stopped
                            return reply
                        inner, envelope_error, describe_text = self._unwrap_tool_use(call)
                        if envelope_error or describe_text:
                            text = envelope_error if envelope_error else describe_text
                            record = self._envelope_result(call, text, success=not envelope_error)
                            output = text
                            contexts = []
                        else:
                            output, record, contexts = self._call_tool(inner)
                        tool_records.append(record)
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": envelope(output, record["provenance"])})
                        batch_contexts.extend(contexts)
                        if record.get("images"):
                            batch_images.extend(record["images"])
                        elif record.get("image"):
                            batch_images.append(record["image"])
                    if batch_contexts:
                        messages.append({"role": "user", "content": envelope("\n".join(batch_contexts), source("tool_data", channel="hook_context"))})
                    for image in batch_images:
                        messages.append({"role": "user", "content": [
                            {"type": "text", "text": envelope(image["caption"], source("tool_data", channel="tool_image"))},
                            {"type": "image_url", "image_url": {"url": image["data_url"]}},
                        ]})
            except Exception as exc:
                # The turn is ending in failure; direction accepted after this
                # point must not be promised to a turn that will not continue.
                self._close_steer()
                stopped = self._interrupted()
                if stopped:
                    reply = stopped
                    failure = stopped
                    return reply
                failure = "Turn failed (" + type(exc).__name__ + ")"
                self._preserve_partial_stream(failed=True)
                details = exc.diagnostics() if isinstance(exc, BackendError) else {}
                failure_meta = self._row_meta(success=False, **details)
                self.log.append("agent", failure, failure_meta)
                raise
            finally:
                self._close_steer()
                self._in_turn.clear()
                self._interrupt.clear()
                self.last_turn_success = success
                self._emit("turn_end", {"message": message, "reply": reply, "success": success,
                                        "failed": not success, "error": failure, "tools": tool_records})
                self._turn_request_id = ""
                if view_deferred:
                    self.view.resume_merges()
                self.view.save()
                if self._plugins_dirty:
                    self._plugins_dirty = False
                    self._reload_plugins()
                    self._reload_remotes()

    def _call_tool(self, call, permitted=True):
        name = call["name"]
        arguments = call.get("arguments", {})
        call_id = call.get("id") or uuid.uuid4().hex
        tool_row = self.log.append("tool", json.dumps({"name": name, "arguments": arguments}),
                        self._row_meta(call_id=call_id, name=name, provenance=source("agent_inference", tool=name)))
        payload, results = self._emit("pre_tool", {"name": name, "tool": name, "id": call_id,
                                                   "arguments": arguments, "approval_managed": True,
                                                   "record_index": tool_row.i,
                                                   "provenance": tool_row.meta["provenance"]})
        blocked = not permitted
        reason = "Tool is not offered on this route" if blocked else ""
        for result in results:
            if isinstance(result, dict) and result.get("block"):
                blocked = True
                reason = str(result.get("reason", "Blocked by pre_tool hook"))
        success = False
        attached = []
        diffs = []
        self.tools.begin_file_changes()
        try:
            if blocked:
                output = "Blocked: " + reason
            else:
                try:
                    if self.delta_sink is not None:
                        self.delta_sink("tool", name)
                    with self._heartbeat("Running " + name):
                        value = self.tools.execute(name, arguments)
                    if isinstance(value, dict):
                        image = value.pop("image", None)
                        extra = value.pop("images", None)
                        if isinstance(image, dict):
                            attached.append(image)
                        if isinstance(extra, list):
                            attached.extend(item for item in extra if isinstance(item, dict))
                    value = self.log.redact(value)
                    output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    success = True
                    if isinstance(value, dict):
                        success = not value.get("error") and value.get("exit_code", 0) in (0, None) and not value.get("timed_out", False)
                except ApprovalRefused as refusal:
                    blocked = True
                    output = "Blocked: " + str(refusal)
                except Exception as exc:
                    output = "Tool error (" + type(exc).__name__ + "): " + str(exc)
        finally:
            diffs = self.tools.take_file_changes()
        output = clip_output(self.log.redact(output), self.config.tools.clip_chars)
        owner = 'built-in'
        if name in self._plugin_tools:
            owner = 'plugin'
        elif name in self._host_tools:
            owner = 'host'
        targets = {}
        for field in ('path', 'url', 'project_id', 'source', 'destination'):
            if isinstance(arguments.get(field), str):
                targets[field] = arguments[field]
        echo_meta = self._row_meta(call_id=call_id, success=success,
                                   blocked=blocked, name=name, diffs=diffs,
                                   provenance=source("tool_data", tool=name, owner=owner, requested=targets, call_id=call_id))
        echo = self.log.append("echo", output, echo_meta)
        echo_meta["provenance"]["record"] = echo.i
        record = {"id": call_id, "name": name, "tool": name, "arguments": arguments,
                  "result": output, "output": output, "success": success, "blocked": blocked,
                  "error": None if success else output, "image": attached[0] if attached else None,
                  "images": attached if success else [], "diffs": diffs,
                  "provenance": echo_meta["provenance"], "record_index": echo.i}
        _, post_results = self._emit("post_tool", record)
        return output, record, self._contexts(results) + self._contexts(post_results)

    @staticmethod
    def _contexts(results):
        out = []
        for result in results:
            if isinstance(result, dict) and result.get("context"):
                out.append(str(result["context"]))
        return out

    def execute_tool(self, name, arguments):
        """Operator-facing direct call through the real registry and approval path."""
        record_id = "operator-" + uuid.uuid4().hex
        output, record, _ = self._call_tool({"id": record_id, "name": name, "arguments": arguments})
        return record

    def status(self):
        debt = self.capture.debt() if hasattr(self.capture, "debt") else []
        return {"messages": self.log.count(), "view_bytes": len(self.view.render().encode("utf-8")),
                "write_debt": len(debt), "agents": self.agents.active()}

    def close(self, end_session=True):
        with self._turn_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.agents.close()
                if end_session:
                    self._emit("session_end", {})
                self.compactor.stop()
                self.view.save()
            finally:
                self.log.close()

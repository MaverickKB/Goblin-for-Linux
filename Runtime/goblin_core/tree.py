"""Immutable binary summaries and dependency-driven compaction queues."""

import copy
import json
import os
from pathlib import Path
from queue import Queue
import threading
import uuid


def utf8_prefix(text, limit):
    """Take at most limit bytes without splitting a Unicode character."""
    return text.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def validate_node(level, index):
    if type(level) is not int or type(index) is not int or level < 0 or index < 0:
        raise ValueError('Node level and index must be nonnegative integers')


class SummaryTree:
    def __init__(self, root: Path, log, node_bytes=512):
        if type(node_bytes) is not int or node_bytes <= 0:
            raise ValueError('node_bytes must be positive')
        self.root = Path(root)
        self.log = log
        self.node_bytes = node_bytes
        self.directory = self.root / 'tree'
        self.directory.mkdir(parents=True, exist_ok=True)
        self._nodes = {}
        self._metadata = {}
        self._write_paths = {}
        self._leaves = Queue()
        self._merges = Queue()
        self._queued = set()
        self._subscribers = []
        self._lock = threading.RLock()
        self.changed = threading.Condition(self._lock)
        self._message_count = log.count()
        for path in sorted(self.directory.glob('*.jsonl')):
            file_level = int(path.name.split('.')[0])
            complete_tail = True
            with path.open('rb') as stream:
                for line in stream:
                    complete_tail = line.endswith(b'\n')
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        if complete_tail:
                            raise
                        # An interrupted final append is uncommitted work.
                        # Preserve that segment and queue the absent node.
                        break
                    key = (record['level'], record['index'])
                    validate_node(*key)
                    if key[0] != file_level:
                        raise ValueError(f'Wrong node level in {path}')
                    text = record['text']
                    if not isinstance(text, str) or len(text.encode('utf-8')) > node_bytes:
                        raise ValueError(f'Invalid node bytes in {path}')
                    if key in self._nodes:
                        raise ValueError(f'Duplicate immutable node in {path}')
                    metadata = self._json_metadata(record.get('meta', {}))
                    self._nodes[key] = text
                    self._metadata[key] = metadata
            if complete_tail:
                self._write_paths[file_level] = path
        # One startup replay seeds missing work. Runtime arrivals only touch
        # their leaf and the newly completed sibling pair.
        self._replay_missing_leaves()
        with self._lock:
            for level, index in self._nodes:
                self._queue_parent(level, index)
        log.subscribe(self.on_message)

    def node(self, level, index):
        validate_node(level, index)
        with self._lock:
            return self._nodes.get((level, index))

    def is_built(self, level, index):
        return self.node(level, index) is not None

    def node_meta(self, level, index):
        """Return detached metadata, or None when the node is unbuilt."""
        validate_node(level, index)
        with self._lock:
            return copy.deepcopy(self._metadata.get((level, index)))

    @staticmethod
    def _json_metadata(meta):
        if not isinstance(meta, dict):
            raise ValueError('Node metadata must be a JSON object')
        # Normalize once so callers cannot mutate nested data after commit,
        # and the in-memory shape equals the shape read back from disk.
        return json.loads(json.dumps(meta, ensure_ascii=False, allow_nan=False))

    def subscribe(self, callback):
        with self._lock:
            self._subscribers.append(callback)

    def ready_leaves(self):
        """Return the thread-safe queue of unbuilt (0, message_index) ids."""
        return self._leaves

    def ready_merges(self):
        """Return the queue of unbuilt parents with both children built."""
        return self._merges

    def _queue(self, key, queue):
        if key not in self._nodes and key not in self._queued:
            self._queued.add(key)
            queue.put(key)
            self.changed.notify_all()

    def _queue_parent(self, level, index):
        sibling = (level, index ^ 1)
        if sibling in self._nodes:
            self._queue((level + 1, index // 2), self._merges)

    def on_message(self, message):
        with self._lock:
            self._message_count = max(self._message_count, message.i + 1)
            if (0, message.i) in self._nodes:
                return
            if len(message.text.encode('utf-8')) > self.node_bytes:
                self._queue((0, message.i), self._leaves)
                return
        self.set_node(0, message.i, message.text)

    def _replay_missing_leaves(self):
        """Collect every missing leaf, then write them in one pass per level."""
        pending = []
        for message in self.log.iter():
            with self._lock:
                self._message_count = max(self._message_count, message.i + 1)
                if (0, message.i) in self._nodes:
                    continue
            if len(message.text.encode('utf-8')) > self.node_bytes:
                with self.changed:
                    self._queue((0, message.i), self._leaves)
                continue
            pending.append({'level': 0, 'index': message.i, 'text': message.text})
        if pending:
            self.set_nodes(pending)

    def _path_for_level(self, level):
        path = self._write_paths.get(level)
        if path is not None:
            return path
        path = self.directory / f'{level}.jsonl'
        if path.exists():
            path = self.directory / f'{level}.{uuid.uuid4().hex}.jsonl'
        self._write_paths[level] = path
        return path

    def set_nodes(self, records):
        """Write many immutable nodes with one append and fsync per level."""
        prepared = []
        keys = []
        with self.changed:
            seen = set()
            for record in records:
                level = record['level']
                index = record['index']
                text = record['text']
                meta = record.get('meta')
                validate_node(level, index)
                key = (level, index)
                if key in seen:
                    raise ValueError('Duplicate node in one batch')
                seen.add(key)
                if not isinstance(text, str) or len(text.encode('utf-8')) > self.node_bytes:
                    raise ValueError('Summary exceeds node_bytes UTF-8 bytes')
                metadata = self._json_metadata(meta) if meta is not None else {}
                if key in self._nodes:
                    if self._nodes[key] != text:
                        raise ValueError('Built nodes are immutable')
                    if meta is not None and self._metadata[key] != metadata:
                        raise ValueError('Built node metadata is immutable')
                    continue
                if (index + 1) * (1 << level) > self._message_count:
                    raise ValueError('Node extends beyond the chat')
                if level > 0:
                    left = (level - 1, index * 2)
                    right = (level - 1, index * 2 + 1)
                    if left not in self._nodes or right not in self._nodes:
                        raise ValueError('Both children must be built first')
                prepared.append({'level': level, 'index': index,
                                 'text': text, 'meta': metadata})
            if not prepared:
                return
            by_level = {}
            for item in prepared:
                by_level.setdefault(item['level'], []).append(item)
            for level in sorted(by_level):
                items = by_level[level]
                items.sort(key=lambda item: item['index'])
                path = self._path_for_level(level)
                with path.open('a', encoding='utf-8') as stream:
                    for item in items:
                        record = {'level': item['level'], 'index': item['index'],
                                  'text': item['text']}
                        if item['meta']:
                            record['meta'] = item['meta']
                        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                for item in items:
                    key = (item['level'], item['index'])
                    self._nodes[key] = item['text']
                    self._metadata[key] = item['meta']
                    self._queued.discard(key)
                    keys.append(key)
            for level, index in keys:
                self._queue_parent(level, index)
            self.changed.notify_all()
            callbacks = tuple(self._subscribers)
        for level, index in keys:
            for callback in callbacks:
                callback(level, index)

    def set_node(self, level, index, text, meta=None):
        record = {'level': level, 'index': index, 'text': text}
        if meta is not None:
            record['meta'] = meta
        self.set_nodes([record])

    def prefix_built(self, upto_i):
        """Check the binary decomposition of a prefix, not every leaf."""
        remaining = upto_i + 1
        start = 0
        with self._lock:
            while remaining:
                level = remaining.bit_length() - 1
                count = 1 << level
                if (level, start // count) not in self._nodes:
                    return False
                start += count
                remaining -= count
        return True

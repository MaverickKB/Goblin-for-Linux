"""Persisted chat coverage with high/low byte-watermark merge batches."""

from fractions import Fraction
from .provenance import stored_lineage
import json
import os
from pathlib import Path
import threading

from .tree import utf8_prefix, validate_node


PLACEHOLDER = '(no summary yet; zoom in for the raw messages)'


class View:
    def __init__(self, root: Path, tree, high_bytes=128000, low_bytes=64000):
        if not 0 < low_bytes < high_bytes:
            raise ValueError('View budgets must satisfy 0 < low_bytes < high_bytes')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tree = tree
        self.high_bytes = high_bytes
        self.low_bytes = low_bytes
        self.path = self.root / 'view.json'
        self.nodes = []
        self._count = 0
        self._compacting = False
        self._merges_deferred = False
        self._lock = threading.RLock()
        if self.path.exists():
            self.load()
        elif tree.log.count():
            self._rebuild_from_log()
        else:
            self.save()
        tree.subscribe(self.on_node)

    def _rebuild_from_log(self):
        """Rebuild one raw leaf per durable record when view.json is missing."""
        self.nodes = []
        self._count = 0
        self._compacting = False
        for message in self.tree.log.iter():
            self.nodes.append((0, message.i))
            self._count += 1
        self.save()
        self._merge_batch()

    def _line(self, level, index):
        text = self.tree.node(level, index)
        if text is None:
            text = PLACEHOLDER
        return self._format_line(level, index, text)

    def _format_line(self, level, index, text):
        count = 1 << level
        # A short raw leaf can include line breaks. Escape those for one line
        # per node; zoom still returns the exact original message.
        text = text.replace('\\', '\\\\').replace('\r', '\\r').replace('\n', '\\n')
        lineage = stored_lineage(self.tree, level, index)
        origins = ','.join(lineage['origins'])
        return f'{index * count}+{count}|[source:{origins}] {text}\n'

    def lines(self):
        """Return detached, oldest-first display rows for the saved frontier.

        Text is the raw node or placeholder. Bytes include the rendered id,
        separator, escaping and newline, so their sum plus the 14-byte chat
        wrapper equals the prompt size. Age rank is zero for the newest row.
        """
        with self._lock:
            rows = []
            newest_rank = len(self.nodes) - 1
            for position, (level, index) in enumerate(self.nodes):
                text = self.tree.node(level, index)
                if text is None:
                    text = PLACEHOLDER
                count = 1 << level
                rendered = self._format_line(level, index, text)
                rows.append({'start': index * count, 'count': count, 'level': level,
                             'text': text, 'bytes': len(rendered.encode('utf-8')),
                             'age_rank': newest_rank - position})
            return rows

    def _prefix_nodes(self, before):
        """Read an earlier boundary through built children without splitting the saved frontier."""
        if before is None:
            return list(self.nodes)
        if type(before) is not int or before < 0 or before > self._count:
            raise ValueError('Invalid conversation boundary')
        selected = []
        for level, index in self.nodes:
            pending = [(level, index)]
            while pending:
                node_level, node_index = pending.pop()
                count = 1 << node_level
                start = node_index * count
                if start >= before:
                    continue
                if start + count <= before:
                    selected.append((node_level, node_index))
                else:
                    child_level = node_level - 1
                    pending.append((child_level, node_index * 2 + 1))
                    pending.append((child_level, node_index * 2))
        return selected

    def _render_nodes(self, nodes):
        lines = ['<chat>\n']
        for level, index in nodes:
            lines.append(self._line(level, index))
        lines.append('</chat>')
        return ''.join(lines)

    def render(self, before=None):
        with self._lock:
            return self._render_nodes(self._prefix_nodes(before))

    def status(self):
        with self._lock:
            pending = 0
            first = self._count
            for level, index in self.nodes:
                if not self.tree.is_built(level, index):
                    if pending == 0:
                        first = index * (1 << level)
                    pending += 1
            size = len(self.render().encode('utf-8'))
            return {'records': self._count, 'nodes': len(self.nodes),
                    'bytes': size, 'high_bytes': self.high_bytes,
                    'low_bytes': self.low_bytes, 'pending': pending,
                    'first_unbuilt': first, 'compacting': self._compacting,
                    'ready': pending == 0 and not self._compacting and size <= self.high_bytes}

    def ready_snapshot(self, before=None):
        with self._lock:
            selected = self._prefix_nodes(before)
            count = self._count if before is None else before
            for level, index in selected:
                if not self.tree.is_built(level, index):
                    return None
            rendered = self._render_nodes(selected)
            size = len(rendered.encode('utf-8'))
            if size > self.high_bytes:
                self._coarsen(selected, size, count)
                rendered = self._render_nodes(selected)
                if len(rendered.encode('utf-8')) > self.high_bytes:
                    return None
            return {'gist': rendered, 'records': count}

    def ready_render(self, before=None):
        snapshot = self.ready_snapshot(before=before)
        return None if snapshot is None else snapshot['gist']

    def compaction_context(self, before, max_bytes):
        with self._lock:
            lines = ['<chat>\n']
            used = len('<chat>\n</chat>'.encode('utf-8'))
            for level, index in self._prefix_nodes(before):
                text = self.tree.node(level, index)
                if text is None:
                    break
                line = self._format_line(level, index, text)
                size = len(line.encode('utf-8'))
                if used + size > max_bytes:
                    break
                lines.append(line)
                used += size
            lines.append('</chat>')
            return ''.join(lines)

    def prompt_render(self, max_bytes):
        """Newest lines that fit. The saved view is not changed.

        A full view is the right prompt for one chat, because the old lines
        stay put and the next call caches them. Several chats on one model
        do not share those lines, so each call would prefill them cold.
        The tail is the compromise. Zoom still opens any omitted line.
        """
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('prompt_bytes must be a positive integer')
        with self._lock:
            wrapper = len('<chat>\n</chat>'.encode('utf-8'))
            selected = []
            used = wrapper
            index = len(self.nodes) - 1
            while index >= 0:
                level, node_index = self.nodes[index]
                line = self._line(level, node_index)
                line_bytes = len(line.encode('utf-8'))
                if used + line_bytes > max_bytes:
                    break
                selected.append(line)
                used = used + line_bytes
                index = index - 1
            selected.reverse()
            omitted = index + 1
            parts = ['<chat>\n']
            if omitted:
                notice = ('Older coverage is omitted from this prompt. '
                          'Zoom a start+count line to open it.\n')
                parts.append(notice)
            for line in selected:
                parts.append(line)
            parts.append('</chat>')
            return ''.join(parts)

    @property
    def needs_compaction(self):
        with self._lock:
            return self._compacting

    def on_message(self, message):
        with self._lock:
            if message.i < self._count:
                return
            if message.i != self._count:
                raise ValueError('View messages must arrive contiguously')
            self.nodes.append((0, message.i))
            self._count += 1
            self.save()
            self._merge_batch()

    def on_node(self, level, index):
        with self._lock:
            self._merge_batch()

    def defer_merges(self):
        """Keep the active turn's frontier while workers build durable nodes."""
        with self._lock:
            self._merges_deferred = True

    def resume_merges(self):
        """Publish ready coarsening after the turn, without waiting for workers."""
        with self._lock:
            self._merges_deferred = False
            self._merge_batch()

    def _merge_batch(self):
        if self._merges_deferred:
            return
        size = len(self.render().encode('utf-8'))
        was_compacting = self._compacting
        if size > self.high_bytes:
            self._compacting = True
        if not self._compacting:
            return
        size, changed = self._coarsen(self.nodes, size, self._count)
        self._compacting = size > self.low_bytes
        # Tree nodes are already durable; checkpoint the completed coarsening batch once.
        if changed or self._compacting != was_compacting:
            self.save()

    def _coarsen(self, nodes, size, records):
        changed = False
        while size > self.low_bytes:
            best_position = None
            best_priority = None
            for position in range(len(nodes) - 1):
                level, index = nodes[position]
                if index % 2 or nodes[position + 1] != (level, index + 1):
                    continue
                if not self.tree.is_built(level + 1, index // 2):
                    continue
                last = (index + 2) * (1 << level) - 1
                priority = Fraction(records - last, 1 << level)
                if best_priority is None or priority > best_priority:
                    best_position = position
                    best_priority = priority
            if best_position is None:
                break
            level, index = nodes[best_position]
            removed = len(self._line(level, index).encode('utf-8'))
            removed += len(self._line(level, index + 1).encode('utf-8'))
            replacement = len(self._line(level + 1, index // 2).encode('utf-8'))
            nodes[best_position:best_position + 2] = [(level + 1, index // 2)]
            size += replacement - removed
            changed = True
        return size, changed

    def save(self):
        with self._lock:
            state = {'version': 1, 'nodes': self.nodes, 'count': self._count,
                     'compacting': self._compacting}
            temporary = self.path.with_suffix('.json.tmp')
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)

    def load(self):
        with self._lock:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if state.get('version') != 1:
                raise ValueError('Unsupported persisted view version')
            nodes = []
            covered = 0
            for level, index in state['nodes']:
                validate_node(level, index)
                count = 1 << level
                if index * count != covered:
                    raise ValueError('Persisted view has a gap or overlap')
                if level > 0 and not self.tree.is_built(level, index):
                    raise ValueError('Persisted view references an unbuilt parent')
                nodes.append((level, index))
                covered += count
            if covered != state['count'] or covered > self.tree.log.count():
                raise ValueError('Persisted view count disagrees with chat')
            self.nodes = nodes
            self._count = covered
            self._compacting = bool(state['compacting'])
            # Recover only a suffix committed to the log before a crash. The
            # saved prefix, including every merge decision, remains intact.
            for message in self.tree.log.iter(start=covered):
                self.on_message(message)

    def zoom(self, start, count, *, offset=0, limit=None):
        if type(start) is not int or type(count) is not int:
            raise ValueError('start and count must be integers')
        if start < 0 or count <= 0 or count & (count - 1) or start % count:
            raise ValueError('Use a power-of-two count and an aligned start')
        if start + count > self.tree.log.count():
            raise IndexError('Range extends beyond the chat')
        if count > 1:
            half = count // 2
            level = half.bit_length() - 1
            return self._line(level, start // half) + self._line(level, start // half + 1)
        raw = self.tree.log.get(start).text.encode('utf-8')
        if type(offset) is not int or offset < 0 or offset > len(raw):
            raise ValueError('Invalid UTF-8 byte offset')
        if offset < len(raw) and raw[offset] & 0xC0 == 0x80:
            raise ValueError('Offset must lie on a UTF-8 character boundary')
        text = raw[offset:].decode('utf-8')
        if limit is None:
            page = utf8_prefix(text, 16000)
            next_offset = offset + len(page.encode('utf-8'))
            if next_offset < len(raw):
                return (page + f'\n[Message {start}: bytes {offset}..{next_offset} of '
                        f'{len(raw)}. Continue with zoom({start}, 1, offset={next_offset}).]')
            return page
        if type(limit) is not int or limit <= 0:
            raise ValueError('Page byte limit must be positive')
        page = utf8_prefix(text, limit)
        if text and not page:
            raise ValueError('Page byte limit cannot fit the next character')
        return page

    def date(self, i):
        return self.tree.log.get(i).date

"""History tools over a session's own append-only transcript.

Every session gets these: the chat log is already the infinity thread, so the
tools read the same ``ChatLog`` the session writes, never a second copy. They
return indices, dates, kinds and snippets, and page a window around a match so a
long transcript stays cheap to consult.
"""

from .tools import object_schema


def _record(message, clip_chars, text=None):
    body = message.text if text is None else text
    body = str(body)
    if len(body) > clip_chars:
        body = body[:clip_chars] + '…'
    return {'i': message.i, 'kind': message.kind, 'date': message.date, 'text': body}


def history_search(log, query, *, limit=20, clip_chars=400):
    """Case-insensitive substring search over the transcript, oldest first."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('history_search needs a non-empty query')
    if not isinstance(limit, int) or limit < 1:
        raise ValueError('history_search limit must be a positive integer')
    needle = query.strip().lower()
    hits = []
    for message in log.iter():
        text = str(message.text)
        at = text.lower().find(needle)
        if at < 0:
            continue
        start = max(0, at - clip_chars // 2)
        snippet = text[start:start + clip_chars]
        if start:
            snippet = '…' + snippet
        hits.append(_record(message, clip_chars, snippet))
        if len(hits) >= limit:
            break
    return {'query': query.strip(), 'count': len(hits), 'matches': hits,
            'note': 'Search stopped at the limit.' if len(hits) >= limit else ''}


def history_read(log, start, count=20, *, clip_chars=400):
    """Read ``count`` consecutive messages beginning at ``start``."""
    if not isinstance(start, int) or start < 0:
        raise ValueError('history_read start must be a non-negative integer')
    if not isinstance(count, int) or count < 1:
        raise ValueError('history_read count must be a positive integer')
    total = log.count()
    if start >= total:
        return {'start': start, 'count': 0, 'messages': [],
                'note': f'Transcript has {total} messages; nothing at {start}.'}
    end = min(total, start + count)
    messages = [_record(log.get(i), clip_chars) for i in range(start, end)]
    return {'start': start, 'count': len(messages), 'messages': messages,
            'transcript_length': total,
            'note': 'More messages follow; raise start to page on.' if end < total else ''}


def history_zoom(log, i, before=5, after=5, *, clip_chars=400):
    """The window around message ``i``: ``before`` earlier and ``after`` later."""
    if not isinstance(i, int) or i < 0:
        raise ValueError('history_zoom index must be a non-negative integer')
    if not isinstance(before, int) or before < 0 or not isinstance(after, int) or after < 0:
        raise ValueError('history_zoom before and after must be non-negative integers')
    total = log.count()
    if i >= total:
        return {'i': i, 'messages': [],
                'note': f'Transcript has {total} messages; nothing at {i}.'}
    start = max(0, i - before)
    end = min(total, i + after + 1)
    messages = [_record(log.get(index), clip_chars) for index in range(start, end)]
    return {'i': i, 'start': start, 'count': len(messages), 'messages': messages,
            'transcript_length': total}


def register_history_tools(registry, log, clip_chars=400):
    """Register the three transcript tools on one session's registry."""
    registry.register(
        'history_search',
        'Search this session\'s own transcript for a substring. Returns message '
        'indices, dates, kinds and snippets, oldest first.',
        object_schema({'query': {'type': 'string'},
                       'limit': {'type': 'integer', 'minimum': 1}}),
        lambda query, limit=20: history_search(log, query, limit=limit,
                                               clip_chars=clip_chars),
        ['query'])
    registry.register(
        'history_read',
        'Read consecutive messages from this session\'s transcript by index.',
        object_schema({'start': {'type': 'integer', 'minimum': 0},
                       'count': {'type': 'integer', 'minimum': 1}}),
        lambda start, count=20: history_read(log, start, count, clip_chars=clip_chars),
        ['start'])
    registry.register(
        'history_zoom',
        'Show the window of transcript messages around one index.',
        object_schema({'i': {'type': 'integer', 'minimum': 0},
                       'before': {'type': 'integer', 'minimum': 0},
                       'after': {'type': 'integer', 'minimum': 0}}),
        lambda i, before=5, after=5: history_zoom(log, i, before, after,
                                                  clip_chars=clip_chars),
        ['i'])

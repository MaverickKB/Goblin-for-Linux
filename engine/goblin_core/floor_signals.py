"""Typed readers for the floor definition.

Each provider turns live state into rows of plain fields. A provider does not
decide thresholds, wording, or whether the floor wakes. Those live on the
rules that read the rows. Field names are the contract in SIGNALS; the
evaluator rejects a rule that names a field the signal does not offer.

The signals here are the events a single Goblin can honestly observe: its own
timers, its own open agenda, and its own failed readings. A signal earns a place
only when this install can produce it.
"""

SIGNALS = {
    'notice': {
        'label': 'Floor error',
        'summary': 'One row per reading the floor failed to take. The row is the exception, not an empty success.',
        'fields': {
            'source': 'Which reading failed, such as floor-definition or intention-tool.',
            'error_type': 'Exception type name.',
            'message': 'Exception message.',
        },
    },
    'timer': {
        'label': 'Timer',
        'summary': 'One row per autonomy timer.',
        'fields': {
            'id': 'Timer id.',
            'slug': 'Session the timer belongs to.',
            'note': 'Timer note.',
            'due': 'Due time in epoch seconds.',
            'repeat': 'Repeat interval in seconds, or null.',
            'created': 'Created time in epoch seconds.',
            'late_seconds': 'How far past due the timer is. Zero until due.',
        },
    },
    'task': {
        'label': 'Background task',
        'summary': 'One row per background task this session started.',
        'fields': {
            'id': 'Task id.',
            'label': 'Task label.',
            'state': 'running, done, failed, stopped, timed_out or lost.',
            'exit_code': 'Exit code when the task ended, or null.',
            'ended': 'Epoch seconds when the task ended, or null while it runs.',
            'running_seconds': 'How long the task ran before it ended, or so far.',
        },
    },
    'agenda_item': {
        'label': 'Agenda line',
        'summary': 'One row per lesson, question, or intention line this session would be shown.',
        'fields': {
            'slug': 'Session slug.',
            'id': 'Lesson, question, or intention id when the line names one.',
            'text': 'The agenda line as the learning store delivers it.',
            'fingerprint': 'Identity of the item, when the store has one.',
            'kind': 'lesson, reminder, question, or intention.',
        },
    },
}


def agenda_rows(learning):
    """One row per open lesson, question, or intention the learning store would show."""
    from .learning import _is_turn_reminder
    rows = []
    for slug in learning.wake_targets():
        for lesson in learning._open_lessons(slug):
            kind = 'reminder' if _is_turn_reminder(lesson) else 'lesson'
            rows.append({
                'slug': str(slug),
                'id': str(lesson.get('id') or ''),
                'text': str(lesson.get('trigger') or ''),
                'fingerprint': str(learning.lesson_fingerprint(lesson)),
                'kind': kind,
            })
        for question in learning._open_questions(slug):
            rows.append({
                'slug': str(slug),
                'id': str(question.get('id') or ''),
                'text': str(question.get('text') or ''),
                'fingerprint': str(learning.item_fingerprint(question)),
                'kind': 'question',
            })
        for intention in learning._open_intentions(slug):
            rows.append({
                'slug': str(slug),
                'id': str(intention.get('id') or ''),
                'text': str(intention.get('title') or ''),
                'fingerprint': str(learning.item_fingerprint(intention)),
                'kind': 'intention',
            })
    return rows


def probe_row(signal):
    """One row of this signal's own types, so a condition is executed at save time.

    Strings stay strings and numbers stay numbers. A comparison the type rejects,
    such as a title against a number, fails here even when the live source is empty.
    """
    samples = {
        'notice': {'source': 'probe', 'error_type': 'probe', 'message': 'probe'},
        'timer': {
            'id': 'probe', 'slug': 'probe', 'note': '', 'due': 0, 'repeat': None,
            'created': 0, 'late_seconds': 0,
        },
        'task': {
            'id': 'probe', 'label': 'probe', 'state': 'done', 'exit_code': 0,
            'ended': 0, 'running_seconds': 0,
        },
        'agenda_item': {
            'slug': 'probe', 'id': 'probe', 'text': 'probe',
            'fingerprint': 'probe', 'kind': 'lesson',
        },
    }
    return dict(samples.get(signal) or {})


def task_rows(tasks, now):
    if not isinstance(tasks, dict):
        return []
    rows = []
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        started = task.get('started')
        ended = task.get('ended')
        running = None
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            stop = ended if isinstance(ended, (int, float)) and not isinstance(ended, bool) else now
            if isinstance(stop, (int, float)) and not isinstance(stop, bool):
                running = max(0, stop - started)
        rows.append({
            'id': str(task.get('id') or ''),
            'label': str(task.get('label') or ''),
            'state': str(task.get('state') or ''),
            'exit_code': task.get('exit_code'),
            'ended': ended,
            'running_seconds': running,
        })
    return rows


def timer_rows(timers, now):
    if not isinstance(timers, dict):
        return []
    rows = []
    for timer in timers.values():
        if not isinstance(timer, dict):
            continue
        due = timer.get('due')
        late = 0
        if now is not None and isinstance(due, (int, float)) and not isinstance(due, bool):
            late = now - due
        rows.append({
            'id': str(timer.get('id') or ''),
            'slug': str(timer.get('slug') or ''),
            'note': str(timer.get('note') or ''),
            'due': due,
            'repeat': timer.get('repeat'),
            'created': timer.get('created'),
            'late_seconds': late,
        })
    return rows

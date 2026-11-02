"""Event-driven wakes: timers, background tasks, approvals and floor rules.

A wake is a turn whose input is an event digest. Delivery waits for the session to
be idle and passes rate caps; events are held, never dropped. See docs/AUTONOMY.md.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from .floor_definition import (
    FloorDefinitionError, active_rule_ids, compose_wake, default_definition,
    effective_world_view, evaluate, exercise_definition,
    load_definition, read_audit, save_definition,
    validate_definition,
)
from .floor_signals import agenda_rows, task_rows, timer_rows
from .learning import LearningStore, intention_titles, OPEN
from .remote import ssh_argv
from .tools import object_schema, AUTO_APPROVE

TASK_FINAL = ('done', 'failed', 'stopped', 'timed_out', 'lost')


def wake_footer(config, definition=None):
    """The autonomy footer for this install, from the floor definition.

    The sentence is editable template text. Names come from configuration, and
    an install that has configured neither gets generic wording. An empty
    template turns the footer off.
    """
    from .floor_definition import render_template
    definition = definition or default_definition()
    return render_template(definition['compose']['footer'], {
        'agent': config.agent_name or 'this session',
        'operator': config.operator_label(),
    }).strip()


def _audit_errors(entries):
    """Bad audit lines, shaped like the other floor errors the page shows."""
    errors = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get('action') != 'error':
            continue
        errors.append({
            'source': entry.get('source') or 'floor-audit',
            'error_type': entry.get('error_type') or 'JSONDecodeError',
            'message': entry.get('error') or 'unreadable audit line',
        })
    return errors


def human_seconds(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f'{seconds}s'
    if seconds < 5400:
        return f'{round(seconds / 60)}m'
    if seconds < 129600:
        return f'{seconds / 3600:.1f}h'
    return f'{seconds / 86400:.1f}d'


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _home_path(home, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(home) / path


def _alive(pid):
    from .procutil import alive
    return alive(pid)


class Autonomy:
    def __init__(self, manager, *, clock=time.time, load_state=True):
        self.manager = manager
        self.config = manager.config
        self.settings = manager.config.autonomy
        self.clock = clock
        self.path = _home_path(self.config.home, self.settings.state_file)
        self.tasks_dir = _home_path(self.config.home, self.settings.tasks_dir)
        self._lock = threading.RLock()
        self._wakeup = threading.Condition(self._lock)
        self._children = {}
        self._approval_threads = {}
        self._production_paused = False
        self._stopping = False
        self._stand_down_requested = False
        self.state = {'timers': {}, 'tasks': {}, 'held': {}, 'wakes': [],
                      'capped': None, 'approvals': {}, 'self_prompt_at': {},
                      'suppress': {}, 'coordination_at': {}, 'reflection_at': {},
                      # The compose path creates these lazily with setdefault(), and the
                      # load loop below only restores keys that are already in this dict.
                      # A key missing here is written to disk by _save() and silently
                      # dropped on the next restart, so the dedup records have to be
                      # declared here rather than discovered at compose time.
                      'agenda_seen': {},
                      'pressure_at': {}, 'pressure_seen': {},
                      'floor_errors': [], 'last_wake': {}, 'rule_fired': {},
                      'last_stand_down': {},
                      # Watched endpoints: when each was last probed, and the answer it
                      # gave. These belong in state for the reason above -- as instance
                      # attributes they would be reset on every restart, and each launch
                      # would re-announce every endpoint's current state as news.
                      'endpoint_checked': {}, 'endpoint_health': {}}
        self._runs = {}
        if load_state and self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            for key in self.state:
                if key in saved:
                    self.state[key] = saved[key]
        for approval in self.state['approvals'].values():
            if approval['state'] == 'pending':
                # The action closure did not survive the restart; the request cannot be honored.
                approval['state'] = 'expired'
            elif approval['state'] == 'approved' and approval.get('result') is None:
                approval['result'] = 'interrupted'
        lesson_path = _home_path(self.config.home, self.settings.lesson_file)
        self.learning = LearningStore(lesson_path, pass_score=self.settings.lesson_pass_score,
                                      max_open=self.settings.max_open_lessons,
                                      fail_limit=self.settings.lesson_fail_limit,
                                      clock=lambda: self.clock())
        self.learning.floor_slug = self.settings.floor_slug
        self._intentions_at = 0.0
        self.thread = None

    def start(self):
        # A new run may stand down again. The previous stop stays in state as the
        # record of why it went quiet, but it must not block the next one.
        self._stand_down_requested = False
        if self.settings.enabled and self.settings.standing_floor and self.settings.self_prompt_enabled:
            self._floor_slug()
        if self.settings.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._run, name='goblin-autonomy', daemon=True)
            self.thread.start()

    def pause_production(self):
        with self._wakeup:
            previous = self._production_paused
            self._production_paused = True
            self._wakeup.notify_all()
            return previous

    def resume_production(self, previously_paused=False):
        with self._wakeup:
            self._production_paused = bool(previously_paused)
            host = getattr(self.manager, 'host', None)
            if self._stopping and getattr(host, 'autonomy', None) is self:
                self._stopping = False
                if self.thread is not None and not self.thread.is_alive():
                    self.thread = None
                self.start()
            self._wakeup.notify_all()

    def active_actions(self):
        with self._lock:
            active = []
            for task in self.state['tasks'].values():
                if task['state'] not in ('running', 'stalled'):
                    continue
                process = self._children.get(task['id'])
                running = process.poll() is None if process is not None else _alive(task['pid'])
                if running:
                    active.append('Background task ' + task['id'])
            if not active:
                self._poll_tasks(self.clock())
            for approval in self.state['approvals'].values():
                if approval['state'] == 'pending':
                    active.append('Pending approval ' + approval['id'])
                elif approval['state'] == 'approved' and approval.get('result') is None:
                    active.append('Approval action ' + approval['id'])
            for approval_id in self._approval_threads:
                label = 'Approval action ' + approval_id
                if label not in active:
                    active.append(label)
            return active

    def request_stand_down(self, hit, now):
        """Close the autonomy switch because a declared floor condition matched.

        The human owns starting autonomy. This path only ever closes the switch,
        and it records which rule and which sentence earned the stop so the
        settings page can say why it went quiet.
        """
        if self._stand_down_requested:
            return
        self._stand_down_requested = True
        self.state['last_stand_down'] = {
            'at': now, 'rule': hit.get('rule') or '', 'text': hit.get('text') or '',
        }
        self._save()
        host = getattr(self.manager, 'host', None)
        if host is None or not hasattr(host, 'set_autonomy'):
            self.settings.enabled = False
            return
        # This runs inside the autonomy thread. Saving settings closes Autonomy,
        # which joins that thread, so the save belongs on its own short thread.
        worker = threading.Thread(target=self._persist_stand_down, args=(host,),
                                  name='goblin-stand-down', daemon=True)
        worker.start()

    def _persist_stand_down(self, host):
        try:
            host.set_autonomy(False, actor='floor')
        except Exception as error:
            # Keep the condition eligible so the next tick tries the save again
            # instead of leaving a switch that recorded a stop and stayed on.
            self._stand_down_requested = False
            print(f'autonomy: stand down save failed ({type(error).__name__})',
                  file=sys.stderr, flush=True)

    def fence(self):
        """Operator Stop fence against autonomous wakes (durable via host when present)."""
        host = getattr(self.manager, 'host', None)
        if host is not None:
            host.operator_stopped = True
            if hasattr(host, '_save_operator_stop'):
                host._save_operator_stop()
            meta = self.manager._metadata.get(host.slug) or {}
            meta['stopped'] = True
            self.manager._metadata[host.slug] = meta
            return
        # Without a host, mark every known session stopped in metadata.
        for slug, meta in list(self.manager._metadata.items()):
            meta = dict(meta)
            meta['stopped'] = True
            self.manager._metadata[slug] = meta

    def _save(self):
        _atomic_json(self.path, self.state)

    def _check_slug(self, slug):
        self.manager.path(slug)
        if slug not in self.manager._metadata:
            raise KeyError(slug)

    def _require_enabled(self):
        if not self.settings.enabled:
            raise RuntimeError('Autonomy is disabled ([autonomy] enabled = false)')

    def post(self, slug, kind, text, **data):
        with self._lock:
            event = {'kind': kind, 'at': self.clock(), 'text': text, 'data': data}
            self.state['held'].setdefault(slug, []).append(event)
            self._save()
            self._wakeup.notify_all()
            return event

    # Timers

    def set_timer(self, slug, note, in_seconds=None, at=None, repeat_seconds=None):
        self._require_enabled()
        self._check_slug(slug)
        settings = self.settings
        now = self.clock()
        if (in_seconds is None) == (at is None):
            raise ValueError('Give exactly one of in_seconds or at')
        if at is not None:
            moment = datetime.fromisoformat(at)
            if moment.tzinfo is None:
                moment = moment.astimezone()
            due = moment.astimezone(timezone.utc).timestamp()
        else:
            due = now + float(in_seconds)
        delay = due - now
        if delay < settings.min_timer_seconds or delay > settings.max_timer_seconds:
            raise ValueError(f'Timer must be {human_seconds(settings.min_timer_seconds)} to '
                             f'{human_seconds(settings.max_timer_seconds)} away')
        if repeat_seconds is not None and repeat_seconds < settings.min_timer_seconds:
            raise ValueError(f'Repeat must be at least {human_seconds(settings.min_timer_seconds)}')
        with self._lock:
            mine = [timer for timer in self.state['timers'].values() if timer['slug'] == slug]
            if len(mine) >= settings.max_timers:
                raise ValueError(f'Timer cap reached ({settings.max_timers}); cancel one first')
            timer = {'id': 't-' + uuid.uuid4().hex[:6], 'slug': slug, 'due': due, 'note': str(note)[:500],
                     'repeat': repeat_seconds, 'created': now}
            self.state['timers'][timer['id']] = timer
            self._save()
            self._wakeup.notify_all()
            return self._timer_view(timer, now)

    def _timer_view(self, timer, now):
        due = datetime.fromtimestamp(timer['due']).astimezone().isoformat(timespec='seconds')
        view = {'id': timer['id'], 'note': timer['note'], 'due': due, 'in': human_seconds(timer['due'] - now)}
        if timer['repeat']:
            view['repeat'] = human_seconds(timer['repeat'])
        return view

    def list_timers(self, slug):
        now = self.clock()
        with self._lock:
            timers = sorted((t for t in self.state['timers'].values() if t['slug'] == slug), key=lambda t: t['due'])
            return [self._timer_view(timer, now) for timer in timers]

    def cancel_timer(self, slug, timer_id):
        with self._lock:
            timer = self.state['timers'].get(timer_id)
            if timer is None or timer['slug'] != slug:
                raise KeyError(f'No timer {timer_id}')
            del self.state['timers'][timer_id]
            self._save()
            return {'cancelled': timer_id}

    # Background tasks

    def start_task(self, slug, label, command, remote=None, cwd=None):
        self._require_enabled()
        self._check_slug(slug)
        with self._lock:
            running = [task for task in self.state['tasks'].values() if task['state'] in ('running', 'stalled')]
            if len(running) >= self.settings.max_tasks:
                raise ValueError(f'Background task cap reached ({self.settings.max_tasks}); stop one first')
            task_id = 'bg-' + uuid.uuid4().hex[:6]
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            output = self.tasks_dir / f'{task_id}.log'
            from .procutil import popen, shell_argv
            if remote:
                settings = self.config.remotes.get(remote)
                if settings is None or not settings.enabled:
                    raise ValueError(f'Unknown or disabled remote: {remote}')
                argv = ssh_argv(settings, self.config.home) + [command]
                directory = str(self.config.cwd)
            else:
                argv = shell_argv(command)
                directory = str(Path(cwd).expanduser() if cwd else self.config.cwd)
            with output.open('wb') as stream:
                process = popen(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT)
            now = self.clock()
            task = {'id': task_id, 'slug': slug, 'label': str(label)[:200], 'command': command, 'remote': remote or '',
                    'cwd': directory, 'pid': process.pid, 'state': 'running', 'started': now, 'ended': None,
                    'exit_code': None, 'output': str(output), 'size': 0, 'last_growth': now}
            self._children[task_id] = process
            self.state['tasks'][task_id] = task
            self._prune_tasks()
            self._save()
            return self._task_view(task)

    def _prune_tasks(self):
        finished = sorted((t for t in self.state['tasks'].values() if t['state'] in TASK_FINAL),
                          key=lambda t: t['ended'] or 0)
        for task in finished[:max(0, len(finished) - self.settings.keep_finished_tasks)]:
            del self.state['tasks'][task['id']]

    def _tail(self, task, chars):
        try:
            with open(task['output'], 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - chars * 4))
                return stream.read().decode('utf-8', errors='replace')[-chars:]
        except FileNotFoundError:
            return ''

    def _task_view(self, task, tail=0):
        end = task['ended'] or self.clock()
        view = {'id': task['id'], 'label': task['label'], 'state': task['state'],
                'elapsed': human_seconds(end - task['started']), 'exit_code': task['exit_code']}
        if task['remote']:
            view['remote'] = task['remote']
        if tail:
            view['output_tail'] = self._tail(task, tail)
        return view

    def task_status(self, slug, task_id=None, tail=2000):
        with self._lock:
            tasks = [t for t in self.state['tasks'].values() if t['slug'] == slug]
            if task_id is None:
                return [self._task_view(task) for task in sorted(tasks, key=lambda t: t['started'])]
            for task in tasks:
                if task['id'] == task_id:
                    if task['state'] in TASK_FINAL:
                        self._drop_task_events(slug, task_id)
                    return self._task_view(task, tail)
            raise KeyError(f'No task {task_id}')

    def _drop_task_events(self, slug, task_id):
        held = self.state['held'].get(slug) or []
        kept = [e for e in held if not (e['kind'] == 'task' and e['data'].get('task_id') == task_id)]
        if len(kept) != len(held):
            self.state['held'][slug] = kept
            self._save()

    def stop_task(self, slug, task_id):
        with self._lock:
            task = self.state['tasks'].get(task_id)
            if task is None or task['slug'] != slug:
                raise KeyError(f'No task {task_id}')
            if task['state'] in TASK_FINAL:
                return self._task_view(task)
            from .procutil import terminate_group
            try:
                terminate_group(task['pid'])
            except ProcessLookupError:
                pass
            task['state'] = 'stopped'
            task['ended'] = self.clock()
            task['notified'] = True
            process = self._children.pop(task_id, None)
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    from .procutil import kill_group
                    kill_group(task['pid'])
                    process.wait()
                task['exit_code'] = process.returncode
            self._save()
            return self._task_view(task)

    def continue_later(self, slug, note):
        self._require_enabled()
        self._check_slug(slug)
        self.post(slug, 'continue', f'Continuation you queued: {str(note)[:2000]}')
        return {'queued': True,
                'note': f'Runs as a wake after this turn ends; reply to {self.config.operator_label()} now if needed.'}

    # Approvals

    def gate(self, runtime, registry, name, arguments, run):
        if registry.allowed and registry.allowed(name):
            return run()
        if registry.approval in AUTO_APPROVE:
            return run()
        if registry.approval == 'ask' and registry.approve and registry.approve(name, arguments):
            return run()
        return self.request_approval(runtime, name, arguments, run)

    def request_approval(self, runtime, name, arguments, run):
        shown = json.dumps(arguments, ensure_ascii=False)
        shown = shown if len(shown) <= 600 else shown[:600] + '...'
        with self._lock:
            approval = {'id': 'ap-' + uuid.uuid4().hex[:8], 'slug': runtime.slug, 'tool': name, 'arguments': shown,
                        'state': 'pending', 'created': self.clock(), 'result': None}
            self.state['approvals'][approval['id']] = approval
            self._runs[approval['id']] = run
            pending = sorted((a for a in self.state['approvals'].values() if a['state'] != 'pending'),
                             key=lambda a: a['created'])
            for old in pending[:max(0, len(pending) - self.settings.keep_finished_tasks)]:
                del self.state['approvals'][old['id']]
            self._save()
        runtime.session.log.append('note', f'{self.config.agent_name} asks to run {name}: {shown}',
                                   {'approval': {'id': approval['id'], 'tool': name, 'state': 'pending'}})
        return {'approval': 'requested', 'id': approval['id'],
                'note': f'{self.config.operator_label_sentence()} sees Approve and Deny. '
                        'You are woken with the outcome. Keep working meanwhile.'}

    def resolve(self, approval_id, approve, source='api'):
        """source 'card' is the operator's chat card; anything else is a direct API call (a script or another agent)."""
        if not isinstance(approve, bool):
            raise ValueError('approve must be true or false')
        with self._lock:
            approval = self.state['approvals'].get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            if approval['state'] != 'pending':
                raise ValueError(f'Approval is already {approval["state"]}')
            approval['state'] = 'approved' if approve else 'denied'
            approval['resolved'] = self.clock()
            approval['by'] = 'card' if source == 'card' else 'api'
            run = self._runs.pop(approval_id, None)
            self._save()
        self._log_result(approval)
        if not approve:
            self.post(approval['slug'], 'approval', f'{self._resolver(approval)} denied {approval["tool"]} ({approval_id}).',
                      approval_id=approval_id, by=approval['by'])
            return {'id': approval_id, 'state': 'denied'}
        thread = threading.Thread(target=self._run_approved, args=(approval, run),
                                  name=f'goblin-approval-{approval_id}', daemon=True)
        with self._lock:
            self._approval_threads[approval_id] = thread
        thread.start()
        return {'id': approval_id, 'state': 'approved'}

    def _resolver(self, approval):
        if approval.get('by') == 'card':
            return f'{self.config.operator_label_sentence()} (from the chat card)'
        return f'Someone through the API, not {self.config.operator_label()}\'s chat card,'

    def _log_result(self, approval):
        runtime = self.manager.runtime(approval['slug'], internal=True)
        runtime.session.log.append('note', f'{approval["tool"]} {approval["state"]}',
                                   {'approval_result': {'id': approval['id'], 'state': approval['state']}})

    def _run_approved(self, approval, run):
        try:
            try:
                value = run()
                if isinstance(value, dict):
                    value = {key: item for key, item in value.items() if key != 'image'}
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
                outcome = 'ran'
            except Exception as error:
                text = f'{type(error).__name__}: {error}'
                outcome = 'failed'
            text = text[-self.settings.wake_output_chars:]
            with self._lock:
                approval['result'] = outcome
                self._save()
            self.post(approval['slug'], 'approval', f'{self._resolver(approval)} approved {approval["tool"]} ({approval["id"]}); '
                      f'it {outcome}. Result: {text}', approval_id=approval['id'], by=approval.get('by', 'api'))
        finally:
            with self._lock:
                self._approval_threads.pop(approval['id'], None)

    def _expire_approvals(self, now):
        for approval in self.state['approvals'].values():
            if approval['state'] == 'pending' and now - approval['created'] > self.settings.approval_expire_seconds:
                approval['state'] = 'expired'
                self._runs.pop(approval['id'], None)
                self._save()
                self._log_result(approval)
                self.post(approval['slug'], 'approval', f'Your request to run {approval["tool"]} ({approval["id"]}) '
                          'expired without an answer.', approval_id=approval['id'])

    # Delivery

    def _cap_reason(self, now):
        settings = self.settings
        wakes = [item for item in (self.state.get('wakes') or []) if isinstance(item, dict)]
        if wakes and now - float(wakes[-1].get('at') or 0) < settings.min_wake_gap_seconds:
            return 'min_wake_gap_seconds'
        if settings.max_wakes_per_hour:
            in_hour = sum(1 for item in wakes if now - float(item.get('at') or 0) < 3600)
            if in_hour >= settings.max_wakes_per_hour:
                return 'max_wakes_per_hour'
        if settings.max_wakes_per_day:
            in_day = sum(1 for item in wakes if now - float(item.get('at') or 0) < 86400)
            if in_day >= settings.max_wakes_per_day:
                return 'max_wakes_per_day'
        return None

    def _deliver(self, now):
        if self._production_paused:
            return
        for slug in list(self.state['held']):
            if not self.state['held'][slug]:
                del self.state['held'][slug]
                continue
            if slug not in self.manager._metadata or self.manager._metadata[slug].get('archived'):
                continue
            events = self._deliverable(slug, now)
            if not events:
                continue
            reason = self._cap_reason(now)
            if reason:
                if not self.state['capped'] or self.state['capped']['reason'] != reason:
                    self.state['capped'] = {'reason': reason, 'since': now, 'held': len(events)}
                    print(f'autonomy: holding {len(events)} event(s) for {slug} ({reason})', file=sys.stderr, flush=True)
                    self._save()
                return
            runtime = self.manager.runtime(slug, internal=True)
            if runtime.operator_stopped or not runtime.idle():
                continue
            held_for = now - min(event['at'] for event in events)
            lines = []
            has_self_prompt = False
            floor_wake = False
            for event in events:
                text = event['text']
                if event['kind'] == 'self_prompt':
                    text = self._refresh_agenda(slug, text, now)
                    if not text:
                        # The agenda was entirely live state that has since settled.
                        continue
                    has_self_prompt = True
                lines.append(f'[wake] {text}')
                if event['kind'] in ('pressure', 'coordination', 'reflection'):
                    floor_wake = True
            if not lines:
                self.state['held'][slug] = [event for event in self.state['held'][slug] if event not in events]
                self._save()
                continue
            session_wake = slug != self.settings.floor_slug
            if not has_self_prompt and session_wake:
                for line in self._agenda_lines(slug):
                    lines.append('[agenda] ' + line)
            if session_wake:
                push = self.learning.improvement_line(slug)
                if push:
                    lines.append(push)
            if self.state['capped'] and held_for >= 1:
                lines.append(f'(Held {human_seconds(held_for)} by wake caps: {self.state["capped"]["reason"]}.)')
            if floor_wake:
                lines.append(wake_footer(self.config))
            sent = '\n'.join(lines)
            rules = []
            for event in events:
                rules.extend((event.get('data') or {}).get('rules') or [])
            wake = {'events': [event['kind'] for event in events], 'held_seconds': round(held_for, 1),
                    'rules': rules, 'text': sent}
            if floor_wake:
                posted = next((event['text'] for event in events
                               if event['kind'] in ('pressure', 'coordination', 'reflection')), '')
                self.state['last_wake'] = {
                    'at': now, 'slug': slug, 'sent': sent, 'posted': posted,
                    'rules': rules, 'kind': wake['events'][-1] if wake['events'] else '',
                }
            turn_id = runtime.enqueue(sent, wake=wake)
            self.state['wakes'].append({'slug': slug, 'at': now, 'turn_id': turn_id, 'kinds': wake['events'],
                                        'spoke': None})
            self.state['wakes'] = [w for w in self.state['wakes'] if now - w['at'] < 86400]
            self.state['held'][slug] = [event for event in self.state['held'][slug] if event not in events]
            self.state['capped'] = None
            self._save()

    def _deliverable(self, slug, now):
        """The held events that may wake now."""
        return list(self.state['held'][slug])

    def _fire_timers(self, now):
        for timer in list(self.state['timers'].values()):
            if timer['due'] > now:
                continue
            late = now - timer['due']
            text = f'Timer {timer["id"]} "{timer["note"]}" is due (set {human_seconds(now - timer["created"])} ago'
            text += f', {human_seconds(late)} late).' if late > 120 else ').'
            self.post(timer['slug'], 'timer', text, timer_id=timer['id'])
            if timer['repeat']:
                while timer['due'] <= now:
                    timer['due'] += timer['repeat']
            else:
                del self.state['timers'][timer['id']]
            self._save()

    def _poll_tasks(self, now):
        settings = self.settings
        for task in list(self.state['tasks'].values()):
            if task['state'] not in ('running', 'stalled'):
                continue
            process = self._children.get(task['id'])
            code = None
            finished = False
            if process is not None:
                code = process.poll()
                finished = code is not None
            elif not _alive(task['pid']):
                finished = True
            try:
                size = os.path.getsize(task['output'])
            except FileNotFoundError:
                size = 0
            if size != task['size']:
                task['size'] = size
                task['last_growth'] = now
                if task['state'] == 'stalled':
                    task['state'] = 'running'
            if finished:
                self._children.pop(task['id'], None)
                task['ended'] = now
                task['exit_code'] = code
                if process is None:
                    task['state'] = 'lost'
                    outcome = 'ended while the core was restarting (exit code unknown)'
                else:
                    task['state'] = 'done' if code == 0 else 'failed'
                    outcome = f'{task["state"]} with exit {code}'
                tail = self._tail(task, settings.wake_output_chars).strip()
                text = f'Background task {task["id"]} "{task["label"]}" {outcome} after {human_seconds(now - task["started"])}.'
                if tail:
                    text += f' Output tail:\n{tail}'
                self.post(task['slug'], 'task', text, task_id=task['id'], state=task['state'])
            elif now - task['started'] > settings.task_max_seconds:
                from .procutil import kill_group
                try:
                    kill_group(task['pid'])
                except ProcessLookupError:
                    pass
                if process is not None:
                    process.wait()
                    task['exit_code'] = process.returncode
                self._children.pop(task['id'], None)
                task['state'] = 'timed_out'
                task['ended'] = now
                self.post(task['slug'], 'task', f'Background task {task["id"]} "{task["label"]}" was killed at the '
                          f'{human_seconds(settings.task_max_seconds)} limit.', task_id=task['id'], state='timed_out')
            elif task['state'] == 'running' and now - task['last_growth'] > settings.task_stall_seconds:
                task['state'] = 'stalled'
                self.post(task['slug'], 'task', f'Background task {task["id"]} "{task["label"]}" has printed nothing for '
                          f'{human_seconds(now - task["last_growth"])}; it is still running.', task_id=task['id'], state='stalled')
            self._save()

    def tick(self, force_floor=False):
        now = self.clock()
        before_wakes = len(self.state.get('wakes') or [])
        before_last = dict(self.state.get('last_wake') or {})
        self._refresh_intentions(now)
        with self._lock:
            self._expire_approvals(now)
            self._poll_tasks(now)
        if self._check_stand_down(now):
            return None
        with self._lock:
            self._fire_timers(now)
        with self._lock:
            # The background loop calls tick() on each tick_seconds. That path
            # keeps the floor interval. force_floor is the explicit wake-now
            # path: it opens the floor timer so this call can compose without
            # waiting out self_prompt_gap_seconds. Delivery caps still apply.
            gap = float(self.settings.self_prompt_gap_seconds or 0)
            if force_floor:
                floor_slug = self.settings.floor_slug
                for kind in ('pressure', 'coordination', 'reflection', 'self_prompt'):
                    last_map = self.state.setdefault(kind + '_at', {})
                    last_map[floor_slug] = now - gap - 1.0
            try:
                self._queue_floor_wake(now, gap)
            except Exception as error:
                print(f'autonomy: floor wake failed ({type(error).__name__}: {error})',
                      file=sys.stderr, flush=True)
            self._queue_self_prompts(now)
            self._deliver(now)
        wakes = self.state.get('wakes') or []
        if len(wakes) > before_wakes:
            return wakes[-1].get('turn_id') or True
        after_last = dict(self.state.get('last_wake') or {})
        if after_last and after_last != before_last:
            return after_last.get('at') or True
        return None

    def _run(self):
        while True:
            with self._wakeup:
                if self._stopping:
                    return
                self._wakeup.wait(self.settings.tick_seconds)
                if self._stopping:
                    return
            try:
                self.tick()
            except Exception as error:
                print(f'autonomy: tick failed ({type(error).__name__}: {error})', file=sys.stderr, flush=True)

    def status(self):
        now = self.clock()
        with self._lock:
            settings = self.settings
            return {'enabled': settings.enabled,
                    'timers': [{'slug': t['slug'], **self._timer_view(t, now)} for t in self.state['timers'].values()],
                    'tasks': [{'slug': t['slug'], **self._task_view(t)} for t in self.state['tasks'].values()],
                    'held': {slug: len(events) for slug, events in self.state['held'].items() if events},
                    'capped': self.state['capped'],
                    'approvals': [{k: a[k] for k in ('id', 'slug', 'tool', 'arguments', 'state')}
                                  for a in self.state['approvals'].values() if a['state'] == 'pending'],
                    'wakes_last_hour': sum(1 for w in self.state['wakes'] if now - w['at'] < 3600),
                    'wakes_last_day': len(self.state['wakes']),
                    'last_wake': float((self.state.get('last_wake') or {}).get('at') or 0),
                    'last_stand_down': dict(self.state.get('last_stand_down') or {}),
                    'learning': self.learning.status(),
                    'suppress': dict(self.state.get('suppress') or {}),
                    'caps': {'min_wake_gap_seconds': settings.min_wake_gap_seconds,
                             'max_wakes_per_hour': settings.max_wakes_per_hour,
                             'max_wakes_per_day': settings.max_wakes_per_day}}

    def _approval_mode(self):
        try:
            status = self.manager.approval_status()
        except Exception as exc:
            self._record_floor_error('approval-status', exc)
            return 'unknown'
        self._clear_floor_error('approval-status')
        if status.get('unleashed') or status.get('yolo'):
            return 'unleashed'
        return 'ask'

    def _read_floor(self, source, read, fallback, errors=(Exception,)):
        """Read one pressure input. A failure is recorded and the fallback is returned."""
        try:
            value = read()
        except errors as exc:
            self._record_floor_error(source, exc)
            return fallback
        self._clear_floor_error(source)
        return value

    def _record_floor_error(self, source, exc):
        record = {'source': source, 'error_type': type(exc).__name__, 'message': str(exc)}
        with self._lock:
            current = [item for item in self.state.get('floor_errors') or [] if item.get('source') != source]
            current.append(record)
            self.state['floor_errors'] = current
            self._save()

    def _clear_floor_error(self, source):
        with self._lock:
            current = list(self.state.get('floor_errors') or [])
            kept = [item for item in current if item.get('source') != source]
            if len(kept) == len(current):
                return
            self.state['floor_errors'] = kept
            self._save()

    def _definition_or_error(self):
        try:
            definition = load_definition(self.config)
        except FloorDefinitionError as exc:
            self._record_floor_error('floor-definition', exc)
            empty = default_definition()
            empty['rules'] = []
            empty['source'] = 'refused'
            return empty, str(exc)
        self._clear_floor_error('floor-definition')
        return definition, ''

    def _collect_rows(self, now):
        """Every signal the definition can name, including ones with no shipped rule."""
        rows = {
            'timer': self._read_floor('timers', lambda: timer_rows(self.state.get('timers'), now), []),
            'task': self._read_floor('tasks', lambda: task_rows(self.state.get('tasks'), now), []),
            'agenda_item': self._read_floor('agenda-items', lambda: agenda_rows(self.learning), []),
            'notice': [dict(item) for item in (self.state.get('floor_errors') or [])],
        }
        return rows

    def _check_stand_down(self, now):
        """Close the switch when a declared condition matches, before this tick speaks.

        This runs before due timers fire, so a deadline rule still sees the timer
        that is about to be delivered, and before the floor composes a wake, so a
        stand-down never spends a model turn on the state it is closing.
        """
        definition, error = self._definition_or_error()
        if error:
            return False
        caught = []
        ran = []
        hits = evaluate(definition, self._collect_rows(now), mode=self._approval_mode(),
                        errors=caught, ran=ran)
        self._apply_rule_errors(ran, caught)
        matched = [hit for hit in hits if hit.get('action') == 'stand_down']
        if not matched:
            return False
        self.request_stand_down(matched[0], now)
        return True

    def _apply_rule_errors(self, ran_ids, caught):
        """Keep one visible error per rule that failed, and clear rules this call ran."""
        failed = {}
        for exc in caught:
            failed['definition-rule:' + str(exc.rule_id)] = exc
        for source, exc in failed.items():
            self._record_floor_error(source, exc)
        ran = {'definition-rule:' + str(rule_id) for rule_id in ran_ids}
        for item in list(self.state.get('floor_errors') or []):
            source = item.get('source') or ''
            if source in ran and source not in failed:
                self._clear_floor_error(source)

    def _retire_inactive_rule_errors(self, definition, mode):
        """Drop failures for rules that are missing or not running in this mode.

        A full reading and a save or import use this. A partial pass does not:
        it clears only the rules it actually ran.
        """
        active = active_rule_ids(definition, mode)
        prefix = 'definition-rule:'
        for item in list(self.state.get('floor_errors') or []):
            source = item.get('source') or ''
            if source.startswith(prefix) and source[len(prefix):] not in active:
                self._clear_floor_error(source)

    def _floor_reading(self, now, definition=None):
        error = ''
        full = definition is None
        if definition is None:
            definition, error = self._definition_or_error()
        mode = self._approval_mode()
        if full and not error:
            self._retire_inactive_rule_errors(definition, mode)
        rows = self._collect_rows(now)
        caught = []
        ran = []
        hits = [] if error else evaluate(definition, rows, mode=mode, errors=caught, ran=ran)
        if not error:
            self._apply_rule_errors(ran, caught)
            if caught:
                rows['notice'] = [dict(item) for item in (self.state.get('floor_errors') or [])]
                notice_rules = [rule for rule in definition['rules'] if rule['signal'] == 'notice']
                if notice_rules:
                    solo = {
                        'version': 1, 'source': 'validated', 'compose': definition['compose'],
                        'sources': definition.get('sources') or {}, 'rules': notice_rules,
                        'draft': definition.get('draft'),
                    }
                    extra = []
                    notice_ran = []
                    notice_hits = evaluate(solo, rows, mode=mode, errors=extra, ran=notice_ran)
                    self._apply_rule_errors(notice_ran, extra)
                    hits = [hit for hit in hits if hit['signal'] != 'notice']
                    hits.extend(notice_hits)
        fired = self.state.get('rule_fired') or {}
        rules = []
        for rule in definition['rules']:
            output = [hit['text'] for hit in hits if hit['rule'] == rule['id']]
            rules.append({
                'id': rule['id'], 'name': rule['name'], 'enabled': rule['enabled'],
                'score': rule['score'], 'wake': rule['wake'], 'lines': output,
                'acting': bool(output) and rule['enabled'] and rule['wake'],
                'last_fired': fired.get(rule['id']),
                'pressure': bool(rule.get('pressure')),
                'emit': rule.get('emit') or 'each',
            })
        world = ''
        rule_error = ''
        if caught:
            rule_error = str(caught[0])
        if not error:
            try:
                _, world, _ = compose_wake(
                    definition, self.config, hits, effective_world_view(definition, self.config))
            except FloorDefinitionError as exc:
                self._record_floor_error('floor-compose', exc)
                rule_error = rule_error or str(exc)
        return {'definition': definition, 'hits': hits, 'rules': rules,
                'world_view': world, 'error': error, 'rows': rows, 'approval_mode': mode,
                'rule_error': rule_error}

    def pressure_report(self):
        """The floor's current hits, ranked.

        Every line a wake would read appears once, with the rule that produced
        it. `steady` reports a quiet reading, not a missing one.
        """
        now = self.clock()
        with self._lock:
            reading = self._floor_reading(now)
            steady = any(hit.get('steady') for hit in reading['hits'])
            # Read the configured floor slug; do not ensure() it. A read must not
            # create a session as a side effect.
            floor_slug = self.settings.floor_slug
            meta = self.manager._metadata.get(floor_slug) or {}
            if not floor_slug or meta.get('archived') or floor_slug not in self.manager._metadata:
                floor_slug = ''
            items = [{'score': round(hit['score'], 2), 'text': hit['text'],
                      'rule': hit['rule'], 'wake': bool(hit['wake'])}
                     for hit in reading['hits'] if isinstance(hit.get('text'), str)]
            return {'steady': bool(steady) and not items,
                    'items': items,
                    'floor_slug': floor_slug,
                    'suppress': dict(self.state.get('suppress') or {}),
                    'wakes_last_hour': sum(1 for w in self.state['wakes'] if now - w['at'] < 3600),
                    'wakes_last_day': len(self.state['wakes']),
                    'learning': self.learning.status(),
                    # The counts above are a summary. These are the items themselves, so
                    # the page can show what the number means instead of the number alone.
                    'open_items': self.learning.open_items(),
                    'rules': reading['rules'],
                    'world_view': reading['world_view'],
                    'errors': list(self.state.get('floor_errors') or []),
                    'last_wake': dict(self.state.get('last_wake') or {}),
                    'definition_error': reading.get('error') or reading.get('rule_error') or '',
                    'approval_mode': reading.get('approval_mode') or ''}

    def floor_view(self):
        """The definition, its live output, the last wake text, and the change log."""
        from .floor_signals import SIGNALS
        from .floor_definition import definition_path
        path = definition_path(self.config)
        error = ''
        raw = ''
        try:
            definition = load_definition(self.config)
        except FloorDefinitionError as exc:
            error = str(exc)
            definition = None
        if path.exists():
            try:
                raw = path.read_text(encoding='utf-8')
            except OSError as exc:
                error = error or ('could not be read (%s)' % exc)
        # A broken file is not replaced with the shipped rules. The editor shows
        # the error and the bytes on disk, and a save is what replaces them.
        audit = read_audit(self.config)
        audit_errors = _audit_errors(audit)
        if definition is None:
            return {
                'definition': None,
                'source': 'invalid',
                'error': error or 'The floor definition file is invalid.',
                'raw': raw,
                'signals': SIGNALS,
                'audit': audit,
                'last_wake': dict(self.state.get('last_wake') or {}),
                'errors': list(self.state.get('floor_errors') or []) + audit_errors,
                'approval_mode': '',
                'preview': None,
            }
        shown = deepcopy(definition)
        shown['compose']['world_view'] = effective_world_view(shown, self.config)
        preview = None
        try:
            report = self.pressure_report()
            preview = {'world_view': report.get('world_view') or '', 'rules': report.get('rules') or [],
                       'items': report.get('items') or []}
            last_wake = report.get('last_wake') or {}
            errors = report.get('errors') or []
            approval_mode = report.get('approval_mode') or ''
        except Exception as exc:
            error = error or str(exc)
            last_wake = dict(self.state.get('last_wake') or {})
            errors = list(self.state.get('floor_errors') or [])
            approval_mode = ''
        return {
            'definition': shown,
            'source': shown.get('source'),
            'error': error,
            'raw': raw,
            'signals': SIGNALS,
            'audit': audit,
            'last_wake': last_wake,
            'errors': list(errors) + audit_errors,
            'approval_mode': approval_mode,
            'preview': preview,
        }

    def floor_save(self, definition, actor='operator'):
        checked = validate_definition(definition)
        now = self.clock()
        rows = self._collect_rows(now)
        exercise_definition(checked, rows)
        stored = save_definition(self.config, checked, actor or 'operator')
        self.config.autonomy.world_view = stored['compose']['world_view']
        self._retire_inactive_rule_errors(stored, self._approval_mode())
        return self.floor_view()

    def close(self):
        with self._wakeup:
            self._stopping = True
            self._wakeup.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError('Autonomy worker is still stopping')
            self.thread = None
        self._stopping = False

    def _agenda_lines(self, slug):
        return list(self.learning.agenda_lines(slug))

    def _suppress_reason(self, slug, now, kind='self_prompt'):
        """Why a due agenda must not call a model, or empty when it may.

        `kind` is what is being armed. Only a held event of that same kind counts as
        already queued: a standing agenda waiting out the wake cap is not a reason to
        silence a coordination wake. A self_prompt held for 76.7 minutes would
        otherwise return 'hold:already_queued' for every coordination wake in
        that window, so unclaimed project work never reaches the floor.

        Reflection is the exception, and it keeps the broad guard on purpose. A
        reflection wake says nothing measured needs attention. That is only true
        when nothing else is already queued for the floor. Arming it beside a held
        self_prompt would deliver both, and the second would contradict the first.
        """
        hold = self._cap_reason(now)
        if hold:
            return 'hold:' + hold
        held = self.state['held'].get(slug) or []
        for event in held:
            if event['kind'] == kind:
                return 'hold:already_queued'
        if kind == 'reflection' and held:
            # Any pending floor wake already answers the question reflection asks.
            return 'hold:already_queued'
        return ''

    def _note_suppress(self, slug, reason):
        previous = (self.state.get('suppress') or {}).get(slug)
        if previous == reason:
            return
        self.state.setdefault('suppress', {})[slug] = reason
        print(f'autonomy: suppressing self_prompt for {slug} ({reason})', file=sys.stderr, flush=True)
        self._save()

    def _intention_tool_name(self, runtime):
        try:
            schemas = runtime.session.tools.schemas()
        except Exception:
            return ''
        for schema in schemas:
            function = schema.get('function') or {}
            name = function.get('name') or ''
            if name.endswith('intention_status'):
                return name
        return ''

    def _refresh_intentions(self, now):
        if not self.settings.enabled or not self.settings.self_prompt_enabled:
            return
        gap = self.settings.self_prompt_gap_seconds
        if self._intentions_at and gap > 0 and now - self._intentions_at < gap:
            return
        runtimes = getattr(self.manager, '_runtimes', {})
        for slug, runtime in list(runtimes.items()):
            name = self._intention_tool_name(runtime)
            if not name:
                continue
            try:
                result = runtime.session.tools.execute(name, {})
            except Exception as exc:
                # The refresh continues for the other sessions. The failure is a floor
                # error, not an empty intention list.
                self._record_floor_error('intention-tool:' + slug, exc)
                continue
            self._clear_floor_error('intention-tool:' + slug)
            titles = intention_titles(result)
            if titles:
                self.learning.adopt_observed(slug, titles)
        self._intentions_at = now

    def _floor_slug(self):
        """The floor session. Floor wakes stay here so the log and layers stay one history."""
        slug = self.settings.floor_slug
        try:
            self.manager.ensure(slug, self.config.floor_label())
        except (ValueError, RuntimeError, OSError):
            return ''
        meta = self.manager._metadata.get(slug) or {}
        if meta.get('archived'):
            return ''
        return slug

    def _arm_or_post(self, slug, now, body, last_map, gap, kind='self_prompt', rules=None):
        reason = self._suppress_reason(slug, now, kind)
        if reason:
            self._note_suppress(slug, reason)
            return
        self.state.setdefault('suppress', {}).pop(slug, None)
        last = last_map.get(slug)
        if last is None:
            last_map[slug] = now
            self._save()
            return
        if gap > 0 and now - last < gap:
            return
        last_map[slug] = now
        self.post(slug, kind, body, rules=list(rules or []))
        return True

    def _queue_self_prompts(self, now):
        if not self.settings.enabled or not self.settings.self_prompt_enabled:
            return
        targets = list(self.learning.wake_targets())
        gap = self.settings.self_prompt_gap_seconds
        last_map = self.state.setdefault('self_prompt_at', {})
        settled_answerable = []
        for slug in targets:
            if slug not in self.manager._metadata or self.manager._metadata[slug].get('archived'):
                continue
            settled = self.learning.settle_answerable(slug)
            seen = (self.state.get('agenda_seen') or {}).get(slug) or {}
            held_ids = self.learning.unchanged_lesson_ids(slug, seen, now, 6 * 3600)
            # Questions and intentions were never deduplicated, so one parked question kept
            # the agenda non-empty forever and woke this session every gap period with the
            # same text. Hold them the same way lessons are held.
            held_ids = held_ids + self.learning.unchanged_item_ids(slug, seen, now, 6 * 3600)
            text_lines = self.learning.agenda_lines(slug, held_ids)
            if not text_lines:
                if held_ids:
                    self._note_suppress(slug, 'agenda_unchanged')
                elif settled:
                    self._note_suppress(slug, 'answerable')
                    settled_answerable.append(slug)
                else:
                    self.state.setdefault('suppress', {}).pop(slug, None)
                continue
            body = 'Standing agenda is due.\n' + '\n'.join(text_lines)
            posted = self._arm_or_post(slug, now, body, last_map, gap)
            if posted:
                record = self.state.setdefault('agenda_seen', {}).setdefault(slug, {})
                for lesson in self.learning._open_lessons(slug):
                    if lesson['id'] in held_ids:
                        continue
                    record[lesson['id']] = {'fp': self.learning.lesson_fingerprint(lesson), 'at': now}
                for item in list(self.learning._open_questions(slug)) + list(self.learning._open_intentions(slug)):
                    if item['id'] in held_ids:
                        continue
                    record[item['id']] = {'fp': self.learning.item_fingerprint(item), 'at': now}
                self._save()
        self._queue_floor_wake(now, gap)

    def _queue_floor_wake(self, now, gap):
        """The floor session responds to the pressure its rules measured.

        The body is the floor definition: rules decide the lines, compose templates
        decide the sentences around them, and a refused definition does not fall
        back to the shipped rules.
        """
        if not self.settings.standing_floor:
            return
        floor_slug = self._floor_slug()
        if not floor_slug:
            return
        reading = self._floor_reading(now)
        if reading['error']:
            return
        runtime = self.manager._runtimes.get(floor_slug)
        if runtime is not None and (runtime.operator_stopped or not runtime.idle()):
            self._note_suppress(floor_slug, 'operator_stopped' if runtime.operator_stopped else 'busy')
            return
        definition = reading['definition']
        hits = list(reading['hits'])
        try:
            kind, text, causing = compose_wake(
                definition, self.config, hits, effective_world_view(definition, self.config))
        except FloorDefinitionError as exc:
            self._record_floor_error('floor-compose', exc)
            return
        last_map = self.state.setdefault(kind + '_at', {})
        # The Floor interval spaces only the idle reflection wake. Pressure and
        # coordination are still held by min_wake_gap_seconds and the hourly and daily caps.
        post_gap = gap if kind == 'reflection' else 0
        if kind != 'reflection':
            # A missing stamp means "remember this kind, do not send". Seed it
            # in the past so the first real fault posts on this call.
            last_map.setdefault(floor_slug, now - max(gap, 1))
        posted = self._arm_or_post(
            floor_slug, now, text, last_map, post_gap, kind=kind, rules=causing)
        if posted:
            self.state['pressure_seen'] = {}
            sessions = reading['rows'].get('session') or []
            self.state['session_busy_seen'] = sorted(
                row['slug'] for row in sessions
                if isinstance(row, dict) and row.get('busy') and isinstance(row.get('slug'), str) and row['slug'])
            fired = self.state.setdefault('rule_fired', {})
            for rule_id in causing:
                fired[rule_id] = now
            self.state['last_wake'] = {
                'at': now, 'slug': floor_slug, 'kind': kind, 'rules': causing,
                'posted': text, 'sent': '',
            }
            self._save()

    def _refresh_agenda(self, slug, text, now):
        """Drop agenda lines for work that settled while the wake was held.

        A self_prompt is composed once and stored as frozen text, then delivered
        whenever the wake caps allow, which can be tens of minutes later. In that
        gap a lesson, question or intention can close, so a held agenda can
        arrive telling the session to resume work that is already finished.

        This is a filter, not a recompute. It removes lines and never adds any,
        and a line whose id cannot be read is kept, because an unreadable id is
        not evidence that the work settled.
        """
        header = 'Standing agenda is due.'
        if not text.startswith(header):
            return text
        body = text[len(header):].lstrip('\n')
        lines = [line for line in body.split('\n') if line]
        lines = self._drop_settled_items(slug, lines)
        if not lines:
            return ''
        return header + '\n' + '\n'.join(lines)

    def _drop_settled_items(self, slug, lines):
        """Drop agenda lines whose lesson, question or intention is no longer open.

        This is a filter, not a recompute. It removes lines and never adds any,
        and a line whose id cannot be read is kept, because an unreadable id is
        not evidence that the work settled.
        """
        lessons = self.learning._lessons
        questions = self.learning._questions
        intentions = self.learning._intentions
        kept = []
        for line in lines:
            kind, _, rest = line.partition(' ')
            item_id, _, _ = rest.partition(':')
            item_id = item_id.strip()
            # A reminder is a lesson row too. learning.agenda_lines emits
            # "Reminder <id>:" for a failed-turn row, with the same id in the
            # same store. Both prefixes are recognized, so a settled reminder
            # leaves the agenda.
            if kind in ('Lesson', 'Reminder'):
                item = lessons.get(item_id)
                if item is not None and item.get('status') not in OPEN:
                    continue
            elif kind == 'Question':
                item = questions.get(item_id)
                if item is not None and item.get('status') != 'open':
                    continue
            elif kind == 'Intention':
                item = intentions.get(item_id)
                if item is not None and item.get('status') != 'committed':
                    continue
            kept.append(line)
        return kept

    def _register_learning_tools(self, runtime, registry, slug, string):
        boolean = {'type': 'boolean'}

        def record_lesson(trigger, failure, check, skill='', evidence_path=''):
            return self.learning.record_lesson(slug, trigger, failure, check, skill=skill, evidence_path=evidence_path)

        def lesson_bind(lesson_id, skill):
            return self.learning.bind_skill(slug, lesson_id, skill)

        def lesson_check(lesson_id, passed, note='', retire=False):
            return self.learning.check(slug, lesson_id, passed, note=note, retire=retire)

        def park_question(text):
            return self.learning.park_question(slug, text)

        def close_question(question_id):
            return self.learning.close_question(slug, question_id)

        def commit_intention(title):
            return self.learning.commit_intention(slug, title)

        def abandon_intention(intention_id, condition):
            return self.learning.abandon_intention(slug, intention_id, condition)

        registry.register('record_lesson', 'Save a lesson Goblin can include in a later wake: trigger, failure, and check. '
                          'Optional skill names the procedure to load next time. Optional evidence_path is a file '
                          'Goblin can check without a model. This is not an AetherMind layer.',
                          object_schema({'trigger': string, 'failure': string, 'check': string, 'skill': string,
                                         'evidence_path': string},
                                        ['trigger', 'failure', 'check']), record_lesson)
        registry.register('lesson_bind', 'Attach a shelf skill to one of your open lessons.',
                          object_schema({'lesson_id': string, 'skill': string}, ['lesson_id', 'skill']), lesson_bind)
        registry.register('lesson_check', 'Record whether a lesson passed. retire=true stops it from waking you.',
                          object_schema({'lesson_id': string, 'passed': boolean, 'note': string, 'retire': boolean},
                                        ['lesson_id', 'passed']), lesson_check)
        registry.register('list_lessons', 'List your open lessons.', object_schema({}),
                          lambda: self.learning.list_lessons(slug))
        registry.register('park_question', 'Leave a standing question Goblin can include in a later wake until you close it.',
                          object_schema({'text': string}, ['text']), park_question)
        registry.register('close_question', 'Close a standing question so it stops waking you.',
                          object_schema({'question_id': string}, ['question_id']), close_question)
        registry.register('commit_intention', 'Commit to one intention. It stays until you abandon it as done, failed, or dropped.',
                          object_schema({'title': string}, ['title']), commit_intention)
        registry.register('abandon_intention', 'Close a committed intention. condition must be done, failed, or dropped.',
                          object_schema({'intention_id': string, 'condition': string}, ['intention_id', 'condition']),
                          abandon_intention)
        registry.register('list_intentions', 'List your committed intentions.', object_schema({}),
                          lambda: self.learning.list_intentions(slug))

    # Tools

    def register_tools(self, runtime, registry):
        registry.defer = None
        if not self.settings.enabled:
            return
        if registry.approve is None:
            registry.defer = lambda name, arguments, run: self.request_approval(runtime, name, arguments, run)
        slug = runtime.slug
        string = {'type': 'string'}
        number = {'type': 'number'}

        def rest(note=''):
            runtime.session.rest_requested = True
            return {'resting': True, 'note': note}

        registry.register('rest', 'Mark this wake as quiet. Suppresses a proactive announcement. Later events can still wake you.',
                          object_schema({'note': string}), rest)
        registry.register('set_timer', 'Set this session\'s own wake timer. It is a tool of this session, not the system floor. '
                          'You are woken with the note when it is due; then follow that note. Use it to keep watching a session, '
                          'process, or job and continue the work. Give in_seconds or an ISO time in at; repeat_seconds makes it recur.',
                          object_schema({'note': string, 'in_seconds': number, 'at': string, 'repeat_seconds': number},
                                        ['note']),
                          lambda note, in_seconds=None, at=None, repeat_seconds=None:
                          self.set_timer(slug, note, in_seconds, at, repeat_seconds))
        registry.register('list_timers', 'List your pending wake timers.', object_schema({}),
                          lambda: self.list_timers(slug))
        registry.register('cancel_timer', 'Cancel one of your wake timers by id.',
                          object_schema({'timer_id': string}, ['timer_id']), lambda timer_id: self.cancel_timer(slug, timer_id))
        remotes = sorted(name for name, remote in self.config.remotes.items() if remote.enabled)

        def background_task(action, command=None, label=None, remote=None, cwd=None, task_id=None, note=None):
            if action == 'start':
                if not command:
                    raise ValueError('start needs command')
                arguments = {'action': 'start', 'command': command, **({'remote': remote} if remote else {})}
                return self.gate(runtime, registry, 'background_task', arguments,
                                 lambda: self.start_task(slug, label or command[:60], command, remote=remote, cwd=cwd))
            if action == 'continue':
                if not note:
                    raise ValueError('continue needs note')
                return self.continue_later(slug, note)
            if action == 'status':
                return self.task_status(slug, task_id)
            if action == 'stop':
                if not task_id:
                    raise ValueError('stop needs task_id')
                return self.stop_task(slug, task_id)
            raise ValueError('action must be start, status, stop or continue')

        properties = {'action': {'type': 'string', 'enum': ['start', 'status', 'stop', 'continue']},
                      'command': string, 'label': string, 'cwd': string, 'task_id': string, 'note': string}
        if remotes:
            properties['remote'] = {'type': 'string', 'enum': remotes}
        registry.register('background_task', 'Run work in the background and get woken when it finishes or stalls. '
                          'start runs a shell command (locally, or on a remote); status/stop take task_id; continue '
                          'queues a note that wakes you after this turn so you can keep working after replying. '
                          'start follows the approval policy; outside Unleashed it asks the operator and wakes you '
                          'with their answer.',
                          object_schema(properties, ['action']), background_task, risky=False)
        self._register_learning_tools(runtime, registry, slug, string)

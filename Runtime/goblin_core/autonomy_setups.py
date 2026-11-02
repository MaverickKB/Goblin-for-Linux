"""Shared autonomy definitions and independent profile-owned execution state."""

from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import uuid

from .autonomy import Autonomy, _atomic_json
from .config import Config, default_settings, normalize_settings
from .floor_definition import (audit_path, default_definition, effective_world_view,
                               exercise_definition, load_definition, validate_definition)


STORAGE_FIELDS = ('state_file', 'tasks_dir', 'lesson_file', 'floor_file')
# The on/off switch belongs to the operator, not to a saved setup. A template
# never carries it, and recalling one leaves the switch where the operator put it.
SWITCH_FIELDS = ('enabled',)


class AutonomySetups:
    def __init__(self, host):
        self.host = host
        self.templates = Path(getattr(host, 'application_home', host.home)) / 'autonomy-templates'
        self.instances = Path(host.home) / 'autonomy' / 'setups'

    def _read(self, path):
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Autonomy setup must be an object: ' + str(path))
        return value

    def _identifier(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise ValueError('Unknown autonomy setup')
        return value

    def _active_id(self):
        current = self.host.autonomy.path.resolve()
        if self.instances.is_dir():
            for directory in sorted(self.instances.iterdir()):
                if (directory / 'setup.json').is_file() and current == (directory / 'state.json').resolve():
                    return directory.name
        return ''

    def list(self):
        with self.host._settings_lock:
            return self._list()

    def _list(self):
        templates = []
        shared_ids = set()
        if self.templates.is_dir():
            for path in sorted(self.templates.glob('*.json')):
                value = self._read(path)
                templates.append({'id': value['id'], 'name': value['name']})
                shared_ids.add(value['id'])
        setups = []
        if self.instances.is_dir():
            for path in sorted(self.instances.glob('*/setup.json')):
                value = self._read(path)
                if value['id'] not in shared_ids:
                    setups.append({'id': value['id'], 'name': value['name']})
        return {'templates': templates, 'setups': setups, 'active_id': self._active_id()}

    def _check_revision(self, expected):
        if expected is not None:
            if type(expected) is not int:
                raise ValueError('expected revision must be an integer')
            if expected != self.host._settings_revision:
                raise RuntimeError('Settings changed since this form was loaded')

    def _definition(self, config, name, identifier):
        floor = load_definition(config)
        world_view = effective_world_view(floor, config)
        floor['compose']['world_view'] = world_view
        autonomy = asdict(config.autonomy)
        autonomy['world_view'] = world_view
        for field in STORAGE_FIELDS + SWITCH_FIELDS:
            autonomy.pop(field, None)
        return {'version': 1, 'id': identifier, 'name': name, 'autonomy': autonomy,
                'world_view': world_view, 'floor_interval_seconds': config.floor_interval_seconds,
                'floor': validate_definition(floor)}

    def _response(self, **fields):
        result = self.list()
        result.update(fields)
        result['snapshot'] = self.host.snapshot()
        return result

    def save(self, name, expected_revision=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Give the autonomy template a name')
        with self.host.session_operation():
            with self.host._settings_lock:
                self._check_revision(expected_revision)
                identifier = uuid.uuid4().hex
                autonomy = self.host.autonomy
                with autonomy._lock:
                    definition = self._definition(autonomy.config, name.strip(), identifier)
                    timers = []
                    for timer in autonomy.state['timers'].values():
                        if timer['slug'] == self.host.slug:
                            timers.append({'note': timer['note'], 'due': timer['due'],
                                           'repeat': timer['repeat']})
                    definition['timers'] = timers
                _atomic_json(self.templates / (identifier + '.json'), definition)
                return self._response(saved_id=identifier)

    def _values(self, definition, directory):
        values = self.host.config.settings_dict()
        autonomy = deepcopy(definition['autonomy'])
        autonomy['state_file'] = str(directory / 'state.json')
        autonomy['tasks_dir'] = str(directory / 'tasks')
        autonomy['lesson_file'] = str(directory / 'lessons.json')
        autonomy['floor_file'] = str(directory / 'floor-definition.json')
        world_view = definition['floor']['compose']['world_view']
        autonomy['world_view'] = world_view
        values['advanced']['autonomy'] = autonomy
        values['world_view'] = world_view
        values['floor_interval_seconds'] = definition['floor_interval_seconds']
        return normalize_settings(values, self.host.config.manifest, self.host.home)

    def _prepare(self, definition):
        identifier = self._identifier(definition['id'])
        directory = self.instances / identifier
        if (directory / 'setup.json').is_file():
            definition['floor'] = self._read(directory / 'floor-definition.json')
        floor = validate_definition(definition['floor'])
        exercise_definition(floor)
        definition['floor'] = floor
        values = self._values(definition, directory)
        config = Config(self.host.home, self.host.config.manifest, values)
        if not (directory / 'setup.json').is_file():
            state = Autonomy(SimpleNamespace(config=config), clock=self.host.autonomy.clock,
                             load_state=False).state
            for timer in definition.get('timers', []):
                for field in ('due', 'repeat'):
                    value = timer[field]
                    if field == 'repeat' and value is None:
                        continue
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError('Timer ' + field + ' must be a finite number')
                    if field == 'repeat' and value < 0:
                        raise ValueError('Timer repeat must be nonnegative')
                if not isinstance(timer['note'], str):
                    raise ValueError('Timer note must be text')
                timer_id = 't-' + uuid.uuid4().hex[:6]
                state['timers'][timer_id] = {
                    'id': timer_id, 'slug': self.host.slug, 'note': timer['note'],
                    'due': timer['due'], 'repeat': timer['repeat'],
                    'created': self.host.autonomy.clock(),
                }
            _atomic_json(directory / 'state.json', state)
            _atomic_json(directory / 'floor-definition.json', floor)
            _atomic_json(directory / 'setup.json', definition)
        # Construction uses the same state and learning parsers as activation.
        Autonomy(SimpleNamespace(config=config), clock=self.host.autonomy.clock)
        load_definition(config)
        return values

    def _retain_current(self):
        autonomy = self.host.autonomy
        identifier = self._active_id() or uuid.uuid4().hex
        directory = self.instances / identifier
        path = directory / 'setup.json'
        name = self._read(path)['name'] if path.is_file() else 'Previous setup'
        definition = self._definition(autonomy.config, name, identifier)
        with autonomy._lock:
            autonomy._save()
            retained = deepcopy(autonomy.state)
            for task in retained['tasks'].values():
                source = Path(task['output'])
                target = directory / 'tasks' / (task['id'] + '.log')
                if source.is_file() and source.resolve() != target.resolve():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    task['output'] = str(target)
            _atomic_json(directory / 'state.json', retained)
            _atomic_json(directory / 'floor-definition.json', definition['floor'])
            source_audit = audit_path(autonomy.config)
            target_audit = directory / 'floor-definition-audit.jsonl'
            if source_audit.is_file() and source_audit.resolve() != target_audit.resolve():
                shutil.copy2(source_audit, target_audit)
            lesson = autonomy.learning.path
            target = directory / 'lessons.json'
            if lesson.is_file() and lesson.resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(lesson, target)
            _atomic_json(path, definition)
        return identifier

    def _activate(self, definition, expected_revision):
        previous = self.host.autonomy
        with self.host.idle_maintenance():
            with self.host._settings_lock:
                self._check_revision(expected_revision)
                if self._active_id() == definition['id']:
                    return self._response()
                values = self._prepare(definition)
                previous.close()
                try:
                    self._retain_current()
                    # Runtime apply issues remain visible in the actual committed
                    # snapshot. Never restart an outgoing writer after commit.
                    self.host.save_settings(values, expected_revision=expected_revision,
                                            floor_definition=definition['floor'])
                finally:
                    if self.host.autonomy is previous and self.host.config is previous.config:
                        previous.start()
                return self._response()

    def select(self, identifier, expected_revision=None):
        identifier = self._identifier(identifier)
        shared = self.templates / (identifier + '.json')
        own = self.instances / identifier / 'setup.json'
        if own.is_file():
            definition = self._read(own)
        elif shared.is_file():
            definition = self._read(shared)
        else:
            raise ValueError('Unknown autonomy setup')
        return self._activate(definition, expected_revision)

    def reset(self, expected_revision=None):
        defaults = default_settings(self.host.config.manifest)
        defaults = normalize_settings(defaults, self.host.config.manifest, self.host.home)
        config = Config(self.host.home, self.host.config.manifest, defaults)
        identifier = uuid.uuid4().hex
        floor = default_definition()
        definition = {'version': 1, 'id': identifier, 'name': 'Shipped defaults',
                      'autonomy': asdict(config.autonomy), 'world_view': config.world_view,
                      'floor_interval_seconds': config.floor_interval_seconds,
                      'floor': floor, 'timers': []}
        for field in STORAGE_FIELDS + SWITCH_FIELDS:
            definition['autonomy'].pop(field, None)
        return self._activate(definition, expected_revision)

"""The floor definition: ordered rules and the wake template, as data.

Code in this module does two generic jobs. It checks that a definition names
real signal fields, and it evaluates one definition against rows those signals
already produced. Thresholds, scores, hold times and sentences live on the
definition. Nothing here special-cases a host, a board, or a display name.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from .floor_signals import SIGNALS, probe_row

_FIELD = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
_OPS = ('==', '!=', '<=', '>=', '<', '>')
_RULE_KEYS = {
    'id', 'name', 'enabled', 'signal', 'when', 'template', 'score', 'wake',
    'emit', 'name_limit', 'name_field', 'action',
    'guard', 'last_fired', 'pressure',
}
_COMPOSE_KEYS = {
    'world_view', 'world_view_max_chars', 'world_view_intro', 'wake_score',
    'stale_seconds', 'stale_label',
    'footer', 'pressure_intro', 'coordination_paragraph', 'quiet_paragraph',
}
_ROW_EXTRAS = ('stale_seconds', 'stale_label')
_JOIN_EXTRAS = ('count', 'names')
_EMITS = ('each', 'join', 'fallback')

# The sentences below are the floor's shipped script. They are data so a person
# can replace them. {agent} and {operator} are filled from this install's config.
_FOOTER = (
    '{agent} is this install\'s continuing session. When a wake names a measured change, '
    'deal with that change and then stop. When nothing needs attention, do not invent work, '
    'do not continue an earlier wake, and do not start a project on your own. If {operator} '
    'left an unfinished request in this conversation, continue only that request. Otherwise wait. '
    'Keep operator Stops and the current tool permissions.'
)
_COORDINATION = (
    'This conversation is the shared chat and autonomy floor for {agent}. '
    'Keep current work aligned with {operator}\u2019s intent. Use configured tools and internal workers '
    'where useful, follow up on their actual results, and preserve work ownership and operator Stops. '
    'Carry load-bearing decisions and unresolved questions through the continuity store and session gist.'
)
_QUIET = (
    'Nothing measured in this wake needs attention. Do not invent a task, an emergency, or a status report. '
    'Wait for the next message unless this conversation already has an unfinished request from the operator.'
)


class FloorDefinitionError(ValueError):
    """A definition the floor refuses to load. The message names the rule and the field."""

    def __init__(self, rule_id, field, message):
        self.rule_id = rule_id
        self.field = field
        super().__init__("Floor definition rule '%s' field '%s': %s" % (rule_id, field, message))


def default_definition():
    """Shipped floor text. Event rules are user data, so this definition ships none."""
    return {
        'version': 1,
        'source': 'shipped',
        'compose': {
            'world_view': '',
            'world_view_max_chars': 2000,
            'world_view_intro': 'What matters to you, in your own words:',
            'wake_score': 1.0,
            'stale_seconds': 6 * 3600,
            'stale_label': 'six hours',
            'footer': _FOOTER,
            'pressure_intro': 'The body measured pressure. Consider it within the wider situation.',
            'coordination_paragraph': _COORDINATION,
            'quiet_paragraph': _QUIET,
        },
        'sources': {name: True for name in SIGNALS},
        'rules': [],
    }


def definition_path(config):
    home = Path(config.home)
    owned = getattr(getattr(config, 'autonomy', None), 'floor_file', '')
    if owned:
        path = Path(owned).expanduser()
        return path if path.is_absolute() else home / path
    profile = getattr(config, 'profile', '') or ''
    if isinstance(profile, str) and profile.strip() and '/' not in profile and '\\' not in profile:
        return home / ('floor-definition-%s.json' % profile.strip())
    return home / 'floor-definition.json'


def audit_path(config):
    path = definition_path(config)
    return path.with_name(path.name.replace('.json', '') + '-audit.jsonl')


def load_definition(config):
    """The saved definition, or the shipped one when this home has not saved one.

    An unreadable or invalid file is refused. It is not replaced with the shipped
    rules, because a broken edit must stay visible.
    """
    path = definition_path(config)
    if not path.exists():
        data = default_definition()
        data['source'] = 'shipped'
        return data
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise FloorDefinitionError('compose', 'file', 'not valid JSON (%s)' % exc) from None
    except OSError as exc:
        raise FloorDefinitionError('compose', 'file', 'could not be read (%s)' % exc) from None
    data = validate_definition(raw)
    data['source'] = 'file'
    return data


def save_definition(config, definition, actor='operator'):
    """Validate, write, and record who changed what. Returns the stored definition.

    The first save is compared with the shipped definition, so an unchanged
    document does not record every rule as added. A condition that validates
    but cannot run against a typed probe row is refused before the write.
    """
    current = None
    path = definition_path(config)
    if path.exists():
        try:
            current = load_definition(config)
        except FloorDefinitionError:
            current = None
    else:
        current = default_definition()
    stored = validate_definition(definition)
    exercise_definition(stored)
    stored['source'] = 'file'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(stored, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    before = validate_definition(current) if current is not None else None
    _audit(config, actor, 'save', before, stored)
    return stored


def validate_definition(raw):
    if not isinstance(raw, dict):
        raise FloorDefinitionError('compose', 'file', 'must be an object')
    extra = set(raw) - {'version', 'source', 'compose', 'rules', 'sources'}
    if extra:
        raise FloorDefinitionError('compose', 'file', 'unknown keys %s' % ', '.join(sorted(extra)))
    if 'compose' not in raw or 'rules' not in raw:
        raise FloorDefinitionError('compose', 'file', 'needs compose and rules')
    compose = _compose(raw['compose'])
    sources = _sources(raw.get('sources'))
    if not isinstance(raw['rules'], list):
        raise FloorDefinitionError('compose', 'rules', 'must be a list')
    rules = []
    seen = set()
    for index, rule in enumerate(raw['rules']):
        label = rule.get('id') if isinstance(rule, dict) else ('#%s' % index)
        if not isinstance(label, str) or not label.strip():
            label = '#%s' % index
        normalized = _rule_of(rule, label, compose)
        if normalized['id'] in seen:
            raise FloorDefinitionError(normalized['id'], 'id', 'is used by another rule')
        seen.add(normalized['id'])
        rules.append(normalized)
    return {'version': 1, 'source': 'validated', 'compose': compose, 'sources': sources, 'rules': rules}


def _compose(raw):
    if not isinstance(raw, dict):
        raise FloorDefinitionError('compose', 'compose', 'must be an object')
    raw = dict(raw)
    raw.pop('disk_free_percent_below', None)
    extra = set(raw) - _COMPOSE_KEYS
    if extra:
        raise FloorDefinitionError('compose', 'compose', 'unknown keys %s' % ', '.join(sorted(extra)))
    missing = _COMPOSE_KEYS - set(raw)
    if missing:
        raise FloorDefinitionError('compose', 'compose', 'missing %s' % ', '.join(sorted(missing)))
    compose = {}
    for key in ('world_view', 'world_view_intro', 'stale_label', 'footer', 'pressure_intro',
                'coordination_paragraph', 'quiet_paragraph'):
        if not isinstance(raw[key], str):
            raise FloorDefinitionError('compose', key, 'must be text')
        compose[key] = raw[key]
    compose['world_view_max_chars'] = _whole('compose', 'world_view_max_chars', raw['world_view_max_chars'], minimum=0)
    compose['stale_seconds'] = _number('compose', 'stale_seconds', raw['stale_seconds'], minimum=0, positive=True)
    compose['wake_score'] = _number('compose', 'wake_score', raw['wake_score'], minimum=None)
    return compose


def _rule_of(raw, label, compose):
    if not isinstance(raw, dict):
        raise FloorDefinitionError(label, 'rule', 'must be an object')
    extra = set(raw) - _RULE_KEYS
    if extra:
        raise FloorDefinitionError(label, 'rule', 'unknown keys %s' % ', '.join(sorted(extra)))
    for key in ('id', 'name', 'enabled', 'signal', 'when', 'template', 'score', 'wake'):
        if key not in raw:
            raise FloorDefinitionError(label, key, 'is required')
    rule_id = raw['id']
    if not isinstance(rule_id, str) or not rule_id.strip() or len(rule_id) > 80:
        raise FloorDefinitionError(label, 'id', 'must be a short name')
    if not isinstance(raw['name'], str):
        raise FloorDefinitionError(rule_id, 'name', 'must be text')
    if not isinstance(raw['enabled'], bool):
        raise FloorDefinitionError(rule_id, 'enabled', 'must be true or false')
    if raw['signal'] not in SIGNALS:
        raise FloorDefinitionError(rule_id, 'signal', 'unknown signal %r' % raw['signal'])
    if not isinstance(raw['when'], str) or not raw['when'].strip():
        raise FloorDefinitionError(rule_id, 'when', 'must be a condition')
    if not isinstance(raw['template'], str):
        raise FloorDefinitionError(rule_id, 'template', 'must be text')
    if not isinstance(raw['wake'], bool):
        raise FloorDefinitionError(rule_id, 'wake', 'must be true or false')
    emit = raw.get('emit', 'each')
    if emit not in _EMITS:
        raise FloorDefinitionError(rule_id, 'emit', 'must be each, join or fallback')
    action = raw.get('action', 'wake')
    if action not in ('wake', 'stand_down'):
        raise FloorDefinitionError(rule_id, 'action', 'must be wake or stand_down')
    if action == 'stand_down' and raw['wake']:
        raise FloorDefinitionError(rule_id, 'wake', 'must be false on a stand_down rule')
    allowed = set(SIGNALS[raw['signal']]['fields']) | set(_ROW_EXTRAS)
    if emit == 'join':
        allowed |= set(_JOIN_EXTRAS)
    _check_expression(rule_id, 'when', raw['when'], allowed - set(_JOIN_EXTRAS))
    _check_template(rule_id, raw['template'], allowed)
    name_field = raw.get('name_field', 'ordinal')
    if emit == 'join' and (not isinstance(name_field, str) or name_field not in SIGNALS[raw['signal']]['fields']):
        raise FloorDefinitionError(rule_id, 'name_field', 'is not a field of %s' % raw['signal'])
    if not isinstance(name_field, str):
        raise FloorDefinitionError(rule_id, 'name_field', 'must be text')
    pressure = raw.get('pressure', False)
    if not isinstance(pressure, bool):
        raise FloorDefinitionError(rule_id, 'pressure', 'must be true or false')
    last = raw.get('last_fired', None)
    if last is not None and (isinstance(last, bool) or not isinstance(last, (int, float))):
        raise FloorDefinitionError(rule_id, 'last_fired', 'must be a time or null')
    guard = raw.get('guard', False)
    if not isinstance(guard, bool):
        raise FloorDefinitionError(rule_id, 'guard', 'must be true or false')
    return {
        'id': rule_id,
        'name': raw['name'],
        'enabled': raw['enabled'],
        'signal': raw['signal'],
        'when': raw['when'].strip(),
        'template': raw['template'],
        'score': _number(rule_id, 'score', raw['score'], minimum=None),
        'wake': raw['wake'],
        'emit': emit,
        'name_limit': _whole(rule_id, 'name_limit', raw.get('name_limit', 5), minimum=1),
        'name_field': name_field,
        'action': action,
        'guard': guard,
        'last_fired': last,
        'pressure': pressure,
    }


def _whole(rule_id, field, value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise FloorDefinitionError(rule_id, field, 'must be an integer')
    if value < minimum:
        raise FloorDefinitionError(rule_id, field, 'must be at least %s' % minimum)
    return value


def _number(rule_id, field, value, minimum=0, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FloorDefinitionError(rule_id, field, 'must be a number')
    if positive and value <= 0:
        raise FloorDefinitionError(rule_id, field, 'must be greater than 0')
    if minimum is not None and value < minimum:
        raise FloorDefinitionError(rule_id, field, 'must be at least %s' % minimum)
    return value


def _check_expression(rule_id, field, source, allowed):
    try:
        node = parse_expression(source)
    except FloorDefinitionError as exc:
        raise FloorDefinitionError(rule_id, field, _bare(exc)) from None
    except ValueError as exc:
        raise FloorDefinitionError(rule_id, field, 'bad token (%s)' % exc) from None
    for name in expression_names(node):
        if name not in allowed:
            raise FloorDefinitionError(rule_id, field, 'unknown field %r' % name)


def _check_template(rule_id, template, allowed):
    for name in _FIELD.findall(template):
        if name not in allowed:
            raise FloorDefinitionError(rule_id, 'template', 'unknown field %r' % name)


def parse_expression(source):
    tokens = _tokenize(source)
    parser = _Parser(tokens)
    node = parser.parse_or()
    if parser.peek() is not None:
        raise FloorDefinitionError('expression', 'when', 'unexpected %r' % parser.peek())
    return node


def expression_names(node):
    kind = node[0]
    if kind == 'name':
        return [node[1]]
    if kind in ('lit',):
        return []
    if kind == 'list':
        names = []
        for item in node[1]:
            names.extend(expression_names(item))
        return names
    names = []
    for child in node[1:]:
        if isinstance(child, tuple):
            names.extend(expression_names(child))
    return names


def eval_expression(source, env):
    return _truth(_eval(parse_expression(source), env))


def render_template(template, env):
    def replace(match):
        name = match.group(1)
        if name not in env:
            raise FloorDefinitionError('template', name, 'has no value')
        value = env[name]
        return '' if value is None else str(value)
    return _FIELD.sub(replace, template)


def row_context(row, compose):
    env = dict(row)
    env['stale_seconds'] = compose['stale_seconds']
    env['stale_label'] = compose['stale_label']
    return env


def _prepare(definition):
    """Validate at most once. A definition already marked validated is used as given."""
    if definition.get('source') == 'validated':
        return definition
    return validate_definition(definition)


def _note_error(errors, exc):
    if errors is not None:
        errors.append(exc)
        return True
    return False


def evaluate(definition, rows_by_signal, *, mode='ask', errors=None, ran=None):
    """Run every enabled rule. One failure skips that rule.

    Validate at most once. Shipped, file, and any other unmarked definition are
    checked on the way in; a definition already marked validated is not checked
    again. A guard runs in Ask when needed and when the mode is unknown. Only Unleashed skips it.
    A rule that fails while it runs is skipped. Pass `errors` to keep the
    FloorDefinitionError; otherwise the failure is raised. `ran` collects the
    id of each rule this call actually executed.
    """
    definition = _prepare(definition)
    compose = definition['compose']
    produced = []

    def note(rule):
        if ran is not None:
            ran.append(rule['id'])

    each = [rule for rule in definition['rules']
            if _active(rule, mode) and source_enabled(definition, rule['signal']) and rule['emit'] == 'each']
    order = []
    for rule in each:
        if rule['signal'] not in order:
            order.append(rule['signal'])
    for signal in order:
        signal_rules = [rule for rule in each if rule['signal'] == signal]
        rows = rows_by_signal.get(signal) or []
        for rule in signal_rules:
            note(rule)
            matched = []
            try:
                for row in rows:
                    if _matches(rule, row, compose):
                        matched.append(_hit(rule, row_context(row, compose)))
            except FloorDefinitionError as exc:
                if _note_error(errors, exc):
                    continue
                raise
            produced.extend(matched)
    for rule in definition['rules']:
        if not _active(rule, mode) or not source_enabled(definition, rule['signal']) or rule['emit'] != 'join':
            continue
        note(rule)
        try:
            matched = [row for row in (rows_by_signal.get(rule['signal']) or []) if _matches(rule, row, compose)]
            if matched:
                produced.append(_join_hit(rule, matched, compose))
        except FloorDefinitionError as exc:
            if _note_error(errors, exc):
                continue
            raise
    for rule in definition['rules']:
        if not _active(rule, mode) or not source_enabled(definition, rule['signal']) or rule['emit'] != 'fallback':
            continue
        if any(hit['signal'] == rule['signal'] and not hit.get('steady') for hit in produced):
            continue
        note(rule)
        try:
            produced.append(_hit(rule, row_context({}, compose), steady=True))
        except FloorDefinitionError as exc:
            if _note_error(errors, exc):
                continue
            raise
    return produced


def exercise_definition(definition, rows_by_signal=None):
    """Run every rule against the live rows and one typed probe row per signal.

    Validation accepts `title < 5` because both sides parse. Executing it does
    not. Save, import, and PUT call this and refuse with the rule and the field.
    """
    definition = _prepare(definition)
    combined = {}
    supplied = rows_by_signal or {}
    for name in SIGNALS:
        rows = list(supplied.get(name) or [])
        rows.append(probe_row(name))
        combined[name] = rows
    errors = []
    evaluate(definition, combined, errors=errors)
    if errors:
        raise errors[0]
    return definition


def compose_wake(definition, config, hits, world_view):
    """The wake body and its kind, from the compose templates and the hits.

    A hit counts as pressure when its rule says so. The signal name does not.
    """
    definition = _prepare(definition)
    compose = definition['compose']
    names = {
        'agent': (getattr(config, 'agent_name', '') or 'this session'),
        'operator': config.operator_label() if hasattr(config, 'operator_label') else 'the operator',
    }
    threshold = compose['wake_score']
    # A stand_down hit closes the autonomy switch instead of speaking. It is
    # handled by the runtime, and never becomes wake text.
    hits = [hit for hit in hits if (hit.get('action') or 'wake') == 'wake']
    pressure = [hit for hit in hits if hit.get('pressure') and hit['wake'] and not hit.get('steady')
                and hit['score'] >= threshold]
    pressure.sort(key=lambda hit: hit['score'], reverse=True)
    # A pressure rule under the wake score is held. It does not become coordination.
    coordination = [hit for hit in hits if hit['wake'] and not hit.get('steady') and not hit.get('pressure')]
    parts = []
    footer = render_template(compose['footer'], names).strip()
    if footer:
        parts.append(footer)
    if pressure:
        intro = compose['pressure_intro'].strip()
        if intro:
            parts.append(intro)
        parts.extend(hit['text'] for hit in pressure)
    view = _clip(world_view, compose['world_view_max_chars'])
    if view:
        intro = compose['world_view_intro'].strip()
        parts.append((intro + '\n' if intro else '') + view)
    if coordination:
        paragraph = render_template(compose['coordination_paragraph'], names).strip()
        if paragraph:
            parts.append(paragraph)
        parts.extend(hit['text'] for hit in coordination)
    if pressure:
        kind = 'pressure'
    elif coordination:
        kind = 'coordination'
    else:
        kind = 'reflection'
        quiet = compose['quiet_paragraph'].strip()
        if quiet:
            parts.append(quiet)
    causing = [hit['rule'] for hit in pressure + coordination]
    return kind, '\n'.join(parts), causing


def effective_world_view(definition, config):
    """World view text for a wake.

    A saved file owns the text. A draft under preview is rendered as written,
    including when that text is empty. Until a file exists, the settings field
    is what the wake reads.
    """
    compose = definition.get('compose') if isinstance(definition, dict) else None
    text = compose.get('world_view') if isinstance(compose, dict) else None
    if definition.get('draft') or definition.get('source') == 'file':
        return text if isinstance(text, str) else ''
    return config.autonomy.world_view or ''


def export_text(definition, fmt='json'):
    definition = validate_definition(definition)
    if fmt == 'json':
        return json.dumps(definition, indent=2, sort_keys=True) + '\n'
    if fmt == 'toml':
        return _to_toml(definition)
    raise FloorDefinitionError('compose', 'format', 'use json or toml')


def import_text(text):
    if not isinstance(text, str) or not text.strip():
        raise FloorDefinitionError('compose', 'file', 'is empty')
    stripped = text.lstrip()
    if stripped.startswith('{'):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FloorDefinitionError('compose', 'file', 'not valid JSON (%s)' % exc) from None
    else:
        import tomllib
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise FloorDefinitionError('compose', 'file', 'not valid TOML (%s)' % exc) from None
    return validate_definition(raw)


def read_audit(config):
    """Every recorded change. A line that is not JSON is returned as an error entry."""
    path = audit_path(config)
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            message = 'line %s is not valid JSON (%s)' % (number, exc)
            rows.append({
                'at': '',
                'actor': 'floor-audit',
                'action': 'error',
                'changes': [{'rule': 'line %s' % number, 'change': message}],
                'error': message,
                'source': 'floor-audit',
                'error_type': 'JSONDecodeError',
            })
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        else:
            message = 'line %s is not an object' % number
            rows.append({
                'at': '',
                'actor': 'floor-audit',
                'action': 'error',
                'changes': [{'rule': 'line %s' % number, 'change': message}],
                'error': message,
                'source': 'floor-audit',
                'error_type': 'JSONDecodeError',
            })
    return rows


def _sources(raw):
    """Every signal has a switch. A missing switch is on. An unknown one is refused."""
    if raw is None:
        return {name: True for name in SIGNALS}
    if not isinstance(raw, dict):
        raise FloorDefinitionError('sources', 'sources', 'must be an object')
    unknown = set(raw) - set(SIGNALS)
    if unknown:
        raise FloorDefinitionError('sources', 'sources', 'unknown source %s' % ', '.join(sorted(unknown)))
    out = {}
    for name in SIGNALS:
        if name not in raw:
            out[name] = True
            continue
        if not isinstance(raw[name], bool):
            raise FloorDefinitionError('sources', name, 'must be true or false')
        out[name] = raw[name]
    return out


def source_enabled(definition, signal):
    """Off stays off in Ask when needed and in Unleashed. A missing switch stays on."""
    sources = definition.get('sources') if isinstance(definition, dict) else None
    if not isinstance(sources, dict) or signal not in sources:
        return True
    return sources[signal] is True


def _active(rule, mode):
    if not rule['enabled']:
        return False
    if rule.get('guard') and mode == 'unleashed':
        return False
    return True


def active_rule_ids(definition, mode):
    """Ids of rules that run in this mode.

    A disabled rule, a rule whose source is off, and a guard in Unleashed are
    absent. Unknown keeps a guard. A missing rule is absent because it is not
    in the definition.
    """
    if not isinstance(definition, dict):
        return set()
    found = set()
    for rule in definition.get('rules') or []:
        if not isinstance(rule, dict) or not isinstance(rule.get('id'), str):
            continue
        if _active(rule, mode) and source_enabled(definition, rule.get('signal')):
            found.add(rule['id'])
    return found


def _bare(exc):
    text = str(exc)
    if isinstance(exc, FloorDefinitionError):
        prefix = "Floor definition rule '%s' field '%s': " % (exc.rule_id, exc.field)
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _matches(rule, row, compose):
    try:
        return eval_expression(rule['when'], row_context(row, compose))
    except FloorDefinitionError as exc:
        raise FloorDefinitionError(rule['id'], 'when', _bare(exc)) from None
    except Exception as exc:
        raise FloorDefinitionError(rule['id'], 'when', 'failed on %r (%s)' % (rule['when'], exc)) from None


def _hit(rule, env, steady=False):
    try:
        text = render_template(rule['template'], env)
    except FloorDefinitionError as exc:
        raise FloorDefinitionError(rule['id'], 'template', _bare(exc)) from None
    return {
        'rule': rule['id'],
        'name': rule['name'],
        'signal': rule['signal'],
        'score': rule['score'],
        'wake': rule['wake'],
        'text': text,
        'steady': steady,
        'enabled': rule['enabled'],
        'pressure': bool(rule.get('pressure')),
        'emit': rule.get('emit') or 'each',
        'action': rule.get('action') or 'wake',
    }


def _join_hit(rule, rows, compose):
    shown = rows[:rule['name_limit']]
    names = ', '.join(str(row.get(rule['name_field'])) for row in shown)
    env = row_context({}, compose)
    env['count'] = len(rows)
    env['names'] = names
    return _hit(rule, env)


def _clip(text, limit):
    view = (text or '').strip()
    if not view:
        return ''
    if not limit or len(view) <= limit:
        return view
    if limit == 1:
        return '\u2026'
    return view[:limit - 1] + '\u2026'


def _audit(config, actor, action, before, after):
    before_rules = {rule['id']: rule for rule in (before or {}).get('rules') or []}
    after_rules = {rule['id']: rule for rule in after['rules']}
    changes = []
    for rule_id in after_rules:
        if rule_id not in before_rules:
            changes.append({'rule': rule_id, 'change': 'added'})
        elif before_rules[rule_id] != after_rules[rule_id]:
            changes.append({'rule': rule_id, 'change': 'edited'})
    for rule_id in before_rules:
        if rule_id not in after_rules:
            changes.append({'rule': rule_id, 'change': 'removed'})
    if (before or {}).get('compose') != after['compose']:
        changes.append({'rule': 'compose', 'change': 'edited'})
    if (before or {}).get('sources') != after.get('sources'):
        changes.append({'rule': 'sources', 'change': 'edited'})
    entry = {
        'at': datetime.now(timezone.utc).isoformat(),
        'actor': actor or 'operator',
        'action': action,
        'changes': changes,
    }
    path = audit_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, sort_keys=True) + '\n')


def _to_toml(definition):
    lines = ['[sources]']
    for key, value in definition.get('sources', {}).items():
        lines.append('%s = %s' % (key, _toml_value(value)))
    lines.append('')
    lines.append('[compose]')
    for key, value in definition['compose'].items():
        lines.append('%s = %s' % (key, _toml_value(value)))
    for rule in definition['rules']:
        lines.append('')
        lines.append('[[rules]]')
        for key, value in rule.items():
            if value is None:
                continue
            lines.append('%s = %s' % (key, _toml_value(value)))
    return '\n'.join(lines) + '\n'


def _toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return '"%s"' % text


def _truth(value):
    if value is None or value is False:
        return False
    if value == '' or value == 0 or value == 0.0:
        return False
    if value == [] or value == ():
        return False
    return True


def _eval(node, env):
    kind = node[0]
    if kind == 'lit':
        return node[1]
    if kind == 'name':
        if node[1] not in env:
            raise FloorDefinitionError('expression', node[1], 'unknown field')
        return env[node[1]]
    if kind == 'not':
        return not _truth(_eval(node[1], env))
    if kind == 'and':
        return _truth(_eval(node[1], env)) and _truth(_eval(node[2], env))
    if kind == 'or':
        return _truth(_eval(node[1], env)) or _truth(_eval(node[2], env))
    if kind == 'list':
        return [_eval(item, env) for item in node[1]]
    if kind == 'in':
        return _eval(node[1], env) in _eval(node[2], env)
    if kind == 'cmp':
        op, left, right = node[1], _eval(node[2], env), _eval(node[3], env)
        if left is None or right is None:
            if op == '==':
                return left is None and right is None or left == right
            if op == '!=':
                return not (left is None and right is None or left == right)
            return False
        if op == '==':
            return left == right
        if op == '!=':
            return left != right
        if op == '<':
            return left < right
        if op == '<=':
            return left <= right
        if op == '>':
            return left > right
        if op == '>=':
            return left >= right
    raise FloorDefinitionError('expression', 'when', 'cannot evaluate')


def _tokenize(source):
    tokens = []
    i = 0
    length = len(source)
    while i < length:
        char = source[i]
        if char.isspace():
            i += 1
            continue
        if char in ('"', "'"):
            quote = char
            i += 1
            chars = []
            while i < length and source[i] != quote:
                if source[i] == '\\' and i + 1 < length:
                    chars.append(source[i + 1])
                    i += 2
                    continue
                chars.append(source[i])
                i += 1
            if i >= length or source[i] != quote:
                raise FloorDefinitionError('expression', 'when', 'unclosed string')
            i += 1
            tokens.append(('str', ''.join(chars)))
            continue
        if char.isdigit() or (char == '.' and i + 1 < length and source[i + 1].isdigit()):
            start = i
            i += 1
            while i < length and (source[i].isdigit() or source[i] == '.'):
                i += 1
            text = source[start:i]
            try:
                number = float(text) if '.' in text else int(text)
            except ValueError:
                raise FloorDefinitionError('expression', 'when', 'bad token %r' % text) from None
            tokens.append(('num', number))
            continue
        if source[i:i + 2] in ('==', '!=', '<=', '>='):
            tokens.append(source[i:i + 2])
            i += 2
            continue
        if char in '()<>,':
            tokens.append(char)
            i += 1
            continue
        if char.isalpha() or char == '_':
            start = i
            i += 1
            while i < length and (source[i].isalnum() or source[i] == '_'):
                i += 1
            tokens.append(source[start:i])
            continue
        raise FloorDefinitionError('expression', 'when', 'unexpected %r' % char)
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        if self.i >= len(self.tokens):
            return None
        token = self.tokens[self.i]
        return token[0] if isinstance(token, tuple) else token

    def take(self):
        token = self.tokens[self.i]
        self.i += 1
        return token

    def parse_or(self):
        left = self.parse_and()
        while self.peek() == 'or':
            self.take()
            left = ('or', left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek() == 'and':
            self.take()
            left = ('and', left, self.parse_not())
        return left

    def parse_not(self):
        if self.peek() == 'not':
            self.take()
            return ('not', self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_primary()
        if self.peek() in _OPS:
            op = self.take()
            return ('cmp', op, left, self.parse_primary())
        if self.peek() == 'in':
            self.take()
            return ('in', left, self.parse_primary())
        return left

    def parse_primary(self):
        token = self.peek()
        if token is None:
            raise FloorDefinitionError('expression', 'when', 'ended early')
        if token == '(':
            self.take()
            if self.peek() == ')':
                self.take()
                return ('list', [])
            first = self.parse_or()
            if self.peek() == ',':
                items = [first]
                while self.peek() == ',':
                    self.take()
                    if self.peek() == ')':
                        break
                    items.append(self.parse_or())
                if self.peek() != ')':
                    raise FloorDefinitionError('expression', 'when', 'unclosed list')
                self.take()
                return ('list', items)
            if self.peek() != ')':
                raise FloorDefinitionError('expression', 'when', 'unclosed group')
            self.take()
            return first
        raw = self.take()
        if isinstance(raw, tuple) and raw[0] == 'str':
            return ('lit', raw[1])
        if isinstance(raw, tuple) and raw[0] == 'num':
            return ('lit', raw[1])
        if raw == 'true':
            return ('lit', True)
        if raw == 'false':
            return ('lit', False)
        if raw == 'null':
            return ('lit', None)
        if isinstance(raw, str) and raw.isidentifier():
            return ('name', raw)
        raise FloorDefinitionError('expression', 'when', 'unexpected %r' % (raw,))


def peek_world_view(config):
    """The world view stored in a saved definition, or None when this home has no file.

    A file that cannot be read raises FloorDefinitionError. The settings page
    shows that failure instead of the settings field as if it were the file.
    """
    path = definition_path(config)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise FloorDefinitionError('compose', 'file', 'could not be read (%s)' % exc) from None
    except json.JSONDecodeError as exc:
        raise FloorDefinitionError('compose', 'file', 'not valid JSON (%s)' % exc) from None
    compose = raw.get('compose') if isinstance(raw, dict) else None
    if not isinstance(compose, dict):
        raise FloorDefinitionError('compose', 'file', 'has no compose object')
    value = compose.get('world_view')
    if not isinstance(value, str):
        raise FloorDefinitionError('compose', 'world_view', 'must be text')
    return value


def staged_world_view(config, text):
    """The definition a world-view save would write, or None when no file exists.

    The file is loaded here, before config is committed. A corrupt file raises
    FloorDefinitionError and the caller leaves config.toml untouched.
    """
    path = definition_path(config)
    if not path.exists():
        return None
    definition = load_definition(config)
    definition['compose']['world_view'] = text
    return definition


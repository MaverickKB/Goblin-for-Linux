"""The shipped floor has compose text and no event rules."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core.floor_definition import FloorDefinitionError, default_definition, evaluate, validate_definition
from goblin_core.floor_signals import SIGNALS


class FloorDefaultTests(unittest.TestCase):
    def test_shipped_floor_has_no_event_rules(self):
        definition = default_definition()
        self.assertEqual(definition['rules'], [])
        self.assertNotIn('disk_free_percent_below', definition['compose'])
        self.assertEqual(set(SIGNALS), {'notice', 'timer', 'task', 'agenda_item'})
        footer = definition['compose']['footer']
        quiet = definition['compose']['quiet_paragraph']
        self.assertNotIn('subminds', footer)
        self.assertNotIn('meaningful pursuits', footer)
        self.assertIn('do not invent work', footer)
        self.assertIn('Nothing measured in this wake needs attention', quiet)

    def test_obsolete_compose_key_is_ignored_and_removed_fields_are_rejected(self):
        definition = default_definition()
        definition['compose'] = dict(definition['compose'], disk_free_percent_below=10)
        stored = validate_definition(definition)
        self.assertNotIn('disk_free_percent_below', stored['compose'])
        definition['rules'] = [{
            'id': 'old-host', 'name': 'Old host rule', 'enabled': True, 'signal': 'host',
            'when': 'not prior_disk_low', 'template': '{label}', 'score': 1, 'wake': True,
        }]
        with self.assertRaises(FloorDefinitionError):
            validate_definition(definition)

    def test_user_rule_matches_rows_the_test_supplies(self):
        definition = default_definition()
        definition['rules'] = [{
            'id': 'overdue-example',
            'name': 'Example overdue timer',
            'enabled': True,
            'signal': 'timer',
            'when': 'late_seconds > 60',
            'template': 'Timer {note} is late by {late_seconds} seconds.',
            'score': 1.3,
            'wake': True,
            'pressure': False,
        }]
        late = {
            'id': 't1', 'slug': 'goblin', 'note': 'check the build', 'due': 0,
            'repeat': None, 'created': 0, 'late_seconds': 120,
        }
        first = evaluate(definition, {'timer': [late]})
        self.assertEqual([hit['rule'] for hit in first if hit['signal'] == 'timer'], ['overdue-example'])
        self.assertIn('check the build', first[0]['text'])
        late['late_seconds'] = 10
        second = evaluate(definition, {'timer': [late]})
        self.assertEqual([hit for hit in second if hit['signal'] == 'timer'], [])


if __name__ == '__main__':
    unittest.main()

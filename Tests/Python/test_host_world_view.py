"""Settings and floor documents share one effective world view."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_host import make_host
from goblin_core.floor_definition import default_definition, save_definition, load_definition, FloorDefinitionError


class HostWorldViewTests(unittest.TestCase):
    def test_unchanged_advanced_mirror_does_not_overwrite_current_floor(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                values = host.snapshot()['settings']
                values['world_view'] = 'Old view'
                values['advanced']['autonomy'] = {'world_view': 'Old view'}
                host.save_settings(values)
                definition = default_definition()
                definition['compose']['world_view'] = 'Edited floor view'
                host.floor_save(json.dumps(definition))
                snapshot = host.snapshot()
                saved = host.save_settings(snapshot['settings'],
                                           expected_revision=snapshot['settings_revision'])
                self.assertEqual(saved['settings']['world_view'], 'Edited floor view')
                self.assertEqual(load_definition(host.config)['compose']['world_view'], 'Edited floor view')
            finally:
                host.shutdown()

    def test_editor_updates_existing_floor_without_changing_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                definition = default_definition()
                definition['compose']['world_view'] = 'Old floor view'
                save_definition(host.config, definition)
                values = host.snapshot()['settings']
                self.assertEqual(values['world_view'], 'Old floor view')
                values['world_view'] = 'Explicit editor view'
                values['advanced']['autonomy'] = {'world_view': 'Old floor view'}
                snapshot = host.save_settings(values)
                self.assertEqual(snapshot['settings_issue'], '')
                self.assertEqual(snapshot['settings']['world_view'], 'Explicit editor view')
                self.assertEqual(host.config.autonomy.world_view, 'Explicit editor view')
                stored = load_definition(host.config)
                self.assertEqual(stored['compose']['world_view'], 'Explicit editor view')
                self.assertEqual(stored['rules'], definition['rules'])
            finally:
                host.shutdown()

    def test_manual_floor_import_remains_effective_on_unrelated_legacy_save(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                legacy = host.config.settings_dict()
                definition = default_definition()
                definition['compose']['world_view'] = 'Manual floor authority'
                save_definition(host.config, definition)
                legacy['agent_name'] = 'Updated name'
                snapshot = host.save_settings(legacy)
                self.assertEqual(snapshot['settings']['world_view'], 'Manual floor authority')
                self.assertEqual(host.config.world_view, 'Manual floor authority')
                self.assertEqual(load_definition(host.config)['compose']['world_view'], 'Manual floor authority')
            finally:
                host.shutdown()

    def test_advanced_world_view_editor_is_respected_when_top_value_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                values = host.config.settings_dict()
                values['advanced']['autonomy'] = {'world_view': 'Advanced editor view'}
                snapshot = host.save_settings(values)
                self.assertEqual(snapshot['settings']['world_view'], 'Advanced editor view')
                self.assertEqual(host.config.autonomy.world_view, 'Advanced editor view')
            finally:
                host.shutdown()

    def test_revisioned_editor_can_replace_manual_floor_with_prior_config_value(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                definition = default_definition()
                definition['compose']['world_view'] = 'Manual imported view'
                save_definition(host.config, definition)
                snapshot = host.snapshot()
                values = snapshot['settings']
                values['world_view'] = host.config.world_view
                saved = host.save_settings(values, expected_revision=snapshot['settings_revision'])
                self.assertEqual(saved['settings']['world_view'], '')
                self.assertEqual(load_definition(host.config)['compose']['world_view'], '')
            finally:
                host.shutdown()

    def test_invalid_saved_floor_rejects_before_config_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                host.save_settings(host.config.settings_dict())
                config_path = Path(temp) / 'config.json'
                before = config_path.read_bytes()
                (Path(temp) / 'floor-definition.json').write_text('{broken json')
                values = host.config.settings_dict()
                values['world_view'] = 'Must not commit'
                with self.assertRaises(FloorDefinitionError):
                    host.save_settings(values)
                self.assertEqual(config_path.read_bytes(), before)
            finally:
                host.shutdown()

    def test_floor_write_failure_is_reported_after_settings_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                save_definition(host.config, default_definition())
                values = host.config.settings_dict()
                values['world_view'] = 'New committed settings'
                with patch('goblin_core.host.save_definition', side_effect=OSError('Injected floor write failure')):
                    snapshot = host.save_settings(values)
                self.assertEqual(host.config.world_view, 'New committed settings')
                self.assertIn('floor definition apply failed', snapshot['settings_issue'])
                self.assertEqual(load_definition(host.config)['compose']['world_view'], '')
                persisted = json.loads((Path(temp) / 'config.json').read_text())
                self.assertEqual(persisted['world_view'], 'New committed settings')
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()

"""Explicit local core activation validates authored state before committing settings."""
import json
import tempfile
import unittest
from pathlib import Path

from test_release_host import make_host, ROOT
from goblin_core.config import ConfigError, default_settings, write_settings
from goblin_core.memory_cores import inspect_core
from goblin_core.vendor_aethermind.aethermind import core


class MemoryTargetValidationTests(unittest.TestCase):
    def test_missing_explicit_core_leaves_settings_capture_and_path_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                host.save_settings(host.config.settings_dict())
                settings_path = Path(temp) / 'config.json'
                before = settings_path.read_bytes()
                capture = host.capture
                target = Path(temp) / 'misspelled-core'
                report = inspect_core(target)
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {'local_project': str(target)}
                with self.assertRaises(ConfigError) as failure:
                    host.save_settings(values)
                self.assertIn(report['issues'][0]['message'], str(failure.exception))
                self.assertEqual(settings_path.read_bytes(), before)
                self.assertIs(host.capture, capture)
                self.assertFalse(target.exists())
            finally:
                host.shutdown()

    def test_unsupported_core_is_rejected_and_authored_copy_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                target = Path(temp) / 'unsupported-core'
                core.AetherMind(target, create=True)
                metadata_path = target / '.aethermind' / 'project.json'
                metadata = json.loads(metadata_path.read_text())
                metadata['format_version'] = 'unknown-future-format'
                metadata_path.write_text(json.dumps(metadata))
                before = {path.name: path.read_bytes() for path in (target / '.aethermind').iterdir() if path.is_file()}
                old_capture = host.capture
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {'local_project': str(target)}
                with self.assertRaisesRegex(ConfigError, 'Unsupported declared format'):
                    host.save_settings(values)
                after = {path.name: path.read_bytes() for path in (target / '.aethermind').iterdir() if path.is_file()}
                self.assertEqual(after, before)
                self.assertIs(host.capture, old_capture)
            finally:
                host.shutdown()

    def test_healthy_native_core_without_derived_tree_can_activate(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                target = Path(temp) / 'existing-core'
                mind = core.AetherMind(target, create=True)
                mind.write_layer(type='discovery', body='Preserve the native history.', ctx='test', conf=1.0, markers=[], author='test')
                report = inspect_core(target)
                self.assertTrue(report['healthy'])
                self.assertTrue(report['derived_tree']['rebuild_pending'])
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {'local_project': str(target)}
                snapshot = host.save_settings(values)
                self.assertEqual(snapshot['settings_issue'], '')
                self.assertEqual(host.capture.project_id, str(target.resolve()))
                self.assertFalse(host.config.aethermind.create_project)
            finally:
                host.shutdown()

    def test_missing_explicit_core_at_startup_keeps_settings_repairable(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'unavailable-core'
            values = default_settings(json.loads((ROOT / 'product.json').read_text()))
            values['advanced'] = {'aethermind': {'local_project': str(missing)}}
            write_settings(Path(temp), ROOT / 'product.json', values)
            host = make_host(Path(temp))
            try:
                self.assertEqual(host.snapshot()['settings']['advanced']['aethermind']['local_project'], str(missing))
                self.assertFalse(missing.exists())
                repaired = host.config.settings_dict()
                repaired['advanced']['aethermind'] = {}
                snapshot = host.save_settings(repaired)
                self.assertEqual(snapshot['settings_issue'], '')
                self.assertEqual(host.config.aethermind.project, str((Path(temp) / 'aethermind').resolve()))
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()

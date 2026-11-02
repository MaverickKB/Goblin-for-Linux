"""Core creation produces independent continuations and preserves authored sources."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))
from goblin_core.memory_cores import create_core, inspect_core
from goblin_core.vendor_aethermind.aethermind import core
from test_release_host import make_host


def snapshot(root):
    files = {}
    for path in root.rglob('*'):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


class MemoryRotationTests(unittest.TestCase):
    def test_empty_core_selection_stays_empty_after_host_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            host = make_host(home)
            try:
                original = Path(host.capture.project_id)
                original_layers = (original / '.aethermind/layers.aem').read_bytes()
                result = create_core(home / 'aethermind-cores', seeded=False, skills_root=home / 'skills')
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {
                    'mode': 'embedded', 'local_project': result['project_root'],
                }
                host.save_settings(values)
                self.assertEqual(host.capture.project_id, result['project_root'])
            finally:
                host.shutdown()
            reopened = make_host(home)
            try:
                self.assertEqual(reopened.capture.project_id, result['project_root'])
                self.assertEqual(reopened._bootstrap['status'], 'external')
                selected = core.AetherMind(Path(result['project_root']), create=False)
                self.assertEqual(selected.read_layers(), [])
                self.assertEqual(selected.read_texture(), '')
                self.assertEqual((original / '.aethermind/layers.aem').read_bytes(), original_layers)
            finally:
                reopened.shutdown()

    def test_empty_core_stays_empty_when_reopened_without_seeding(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            result = create_core(parent / 'cores', seeded=False, skills_root=parent / 'skills')
            root = Path(result['project_root'])
            self.assertIsNone(result['bootstrap'])
            self.assertTrue(result['inspection']['healthy'])
            before = snapshot(root)
            for _ in range(2):
                mind = core.AetherMind(root, create=False)
                self.assertEqual(mind.read_layers(), [])
                self.assertEqual(mind.read_texture(), '')
                self.assertEqual(inspect_core(root)['currentness']['history_count'], 0)
                self.assertFalse(inspect_core(root)['derived_tree']['rebuild_pending'])
            self.assertEqual(snapshot(root), before)
            self.assertFalse((parent / 'skills').exists())

    def test_seeded_and_empty_choices_preserve_original_core_and_custom_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            original = parent / 'original'
            mind = core.AetherMind(original, create=True)
            mind.write_layer(type='discovery', body='Original authored experience.', ctx='work', conf=1.0, markers=[], author='test')
            mind.write_texture('Original texture.', 'work', author='test')
            original_bytes = snapshot(original)
            skills = parent / 'skills'
            pack = json.loads((ROOT / 'Resources/Seeds/pack.json').read_text())
            custom = skills / pack['skills'][0]
            custom.mkdir(parents=True)
            (custom / 'SKILL.md').write_text('User customization must be retained.')
            custom_bytes = snapshot(custom)
            shelf_bytes = snapshot(skills)
            seeded = create_core(parent / 'cores', seeded=True, skills_root=skills)
            empty = create_core(parent / 'cores', seeded=False, skills_root=skills)
            self.assertNotEqual(seeded['project_root'], empty['project_root'])
            self.assertEqual(seeded['bootstrap']['status'], 'ready')
            seeded_mind = core.AetherMind(seeded['project_root'], create=False)
            self.assertEqual({layer.body for layer in seeded_mind.read_layers()}, {layer['body'] for layer in pack['layers']})
            self.assertEqual(core.AetherMind(empty['project_root'], create=False).read_layers(), [])
            self.assertEqual(snapshot(original), original_bytes)
            self.assertEqual(snapshot(custom), custom_bytes)
            self.assertEqual(snapshot(skills), shelf_bytes)
            self.assertEqual(seeded['bootstrap']['skills_staged'], 0)

    def test_failed_foundation_creation_removes_only_its_new_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            original = parent / 'existing'
            core.initialize_store(original)
            before = snapshot(original)
            with patch('goblin_core.bootstrap_seeds.ensure_bootstrap', return_value={'status': 'unavailable', 'error': 'Missing foundation assets'}):
                with self.assertRaisesRegex(ValueError, 'Missing foundation assets'):
                    create_core(parent, seeded=True, skills_root=parent / 'skills')
            self.assertEqual(snapshot(original), before)
            self.assertEqual(list(parent.glob('core-*')), [])

    def test_seeded_core_does_not_create_missing_shared_shelf(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            shelf = parent / 'skills'
            result = create_core(parent / 'cores', seeded=True, skills_root=shelf)
            self.assertGreater(result['bootstrap']['layers_appended'], 0)
            self.assertFalse(shelf.exists())

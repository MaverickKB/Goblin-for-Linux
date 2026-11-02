"""Native foundations populate the owned store without changing user memory."""
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core.aethermind import AetherMindClient
from goblin_core.aethermind_direct import DirectMcpClient
from goblin_core.bootstrap_seeds import ensure_bootstrap
from goblin_core.capture import Capture
from goblin_core.vendor_aethermind.aethermind.core import AetherMind

ASSETS = ROOT / 'Resources' / 'Seeds'


def install_foundations(arguments):
    project, shelf = arguments
    return ensure_bootstrap(project, shelf, assets_root=ASSETS)


class BootstrapSeedTests(unittest.TestCase):
    def test_native_host_bootstraps_before_its_first_foreground_request(self):
        from test_host import make_host
        from test_session import FakeBackend
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            backend = FakeBackend([])
            host.session._backend_provider = lambda: backend
            try:
                foundation = host.snapshot()['memory']['foundation']
                self.assertEqual(foundation['status'], 'ready', foundation)
                host.session.run_turn('Help me use this application.')
                contents = []
                for message in backend.messages[0]:
                    content = message.get('content', '')
                    if isinstance(content, str):
                        contents.append(content)
                request_text = '\n'.join(contents)
                self.assertIn('Goblin is the user-configured native macOS companion', request_text)
                self.assertIn('general product and working knowledge', request_text)
            finally:
                host.shutdown()

    def pack(self):
        return json.loads((ASSETS / 'pack.json').read_text())

    def test_first_capture_inherits_product_foundation_without_model_or_setup_verb(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project = home / 'aethermind'
            result = ensure_bootstrap(project, home / 'skills', assets_root=ASSETS)
            self.assertEqual(result['status'], 'ready', result)
            self.assertEqual(result['layers_appended'], len(self.pack()['layers']))
            self.assertEqual(result['textures_appended'], len(self.pack()['textures']))
            client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
            capture = Capture(client, home / 'outbox', str(project), author='fixture')
            inherited = capture.session_start({'session_id': 'first-session'})['context']
            self.assertIn('Goblin is the user-configured native macOS companion', inherited)
            self.assertIn('governing pstack', inherited)
            self.assertIn('Bootstrap content supplies general product and working knowledge', inherited)
            layers = AetherMind(project).read_layers()
            contexts = {layer.ctx for layer in layers}
            self.assertIn('seed/goblin/settings', contexts)
            self.assertIn('seed/goblin/world-and-floor', contexts)
            self.assertIn('seed/goblin/model-roles', contexts)

    def test_first_foreground_request_contains_real_inherited_foundations(self):
        from test_session import FakeBackend, make_session
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            ensure_bootstrap(home / 'aethermind', home / 'skills', assets_root=ASSETS)
            backend = FakeBackend([])
            session, config = make_session(temp, backend)
            config.model = 'fixture-model'
            try:
                session.run_turn('Help me use this application.')
                foreground = []
                for request in backend.messages:
                    for message in request:
                        content = message.get('content', '')
                        if isinstance(content, str) and '<turn-state>' in content:
                            foreground.append(content)
                self.assertEqual(len(foreground), 1)
                self.assertIn('Goblin is the user-configured native macOS companion', foreground[0])
                self.assertIn('general product and working knowledge', foreground[0])
            finally:
                session.close()

    def test_second_start_preserves_exact_bytes_user_records_and_custom_skill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'owned'
            shelf = root / 'skills'
            mind = AetherMind(project, create=True)
            user = mind.write_layer(type='discovery', body='A real user-authored note.',
                                    ctx='user/actual-experience', author='user', conf=0.9, markers=[])
            custom = shelf / 'first-contact' / 'SKILL.md'
            custom.parent.mkdir(parents=True)
            custom.write_text('My own procedure remains unchanged.')
            first = ensure_bootstrap(project, shelf, assets_root=ASSETS)
            self.assertEqual(first['status'], 'ready', first)
            before = (project / '.aethermind' / 'layers.aem').read_bytes()
            texture = (project / '.aethermind' / 'texture.aem').read_bytes()
            second = ensure_bootstrap(project, shelf, assets_root=ASSETS)
            self.assertEqual(second['status'], 'ready', second)
            self.assertEqual(second['layers_appended'], 0)
            self.assertEqual(second['textures_appended'], 0)
            self.assertEqual(second['skills_staged'], 0)
            self.assertEqual(before, (project / '.aethermind' / 'layers.aem').read_bytes())
            self.assertEqual(texture, (project / '.aethermind' / 'texture.aem').read_bytes())
            self.assertEqual(custom.read_text(), 'My own procedure remains unchanged.')
            restored = AetherMind(project).read_layers()
            self.assertEqual(restored[0].id, user.id)
            self.assertEqual(restored[0].body, user.body)

    def test_concurrent_processes_install_one_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            arguments = (str(root / 'owned'), str(root / 'skills'))
            context = multiprocessing.get_context('spawn')
            with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
                first = executor.submit(install_foundations, arguments)
                second = executor.submit(install_foundations, arguments)
                results = [first.result(timeout=20), second.result(timeout=20)]
            self.assertEqual([item['status'] for item in results], ['ready', 'ready'])
            self.assertEqual(sum(item['layers_appended'] for item in results), len(self.pack()['layers']))
            self.assertEqual(sum(item['textures_appended'] for item in results), len(self.pack()['textures']))
            self.assertEqual(len(AetherMind(arguments[0]).read_layers()), len(self.pack()['layers']))

    def test_invalid_store_is_preserved_without_quarantine_or_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = root / 'owned' / '.aethermind'
            store.mkdir(parents=True)
            original = b'not a supported store\n'
            (store / 'layers.aem').write_bytes(original)
            result = ensure_bootstrap(store.parent, root / 'skills', assets_root=ASSETS)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual((store / 'layers.aem').read_bytes(), original)
            self.assertFalse(list(store.parent.glob('.aethermind.corrupt-*')))
            self.assertFalse((root / 'skills').exists())

    def test_native_resource_path_and_missing_asset_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {'GOBLIN_RESOURCES': str(ROOT / 'Resources')}):
                result = ensure_bootstrap(root / 'owned', root / 'skills')
            self.assertEqual(result['status'], 'ready', result)
            missing = ensure_bootstrap(root / 'other', root / 'other-skills', assets_root=root / 'missing')
            self.assertEqual(missing['status'], 'unavailable')
            self.assertEqual(missing['error_class'], 'FileNotFoundError')
            self.assertFalse((root / 'other').exists())

    def test_inventory_accounts_for_upstream_entries_and_every_shipped_foundation(self):
        manifest = json.loads((ASSETS / 'SOURCE.json').read_text())
        inventory = manifest['adaptation_inventory']
        source_layers = []
        source_textures = []
        source_skills = []
        targets = set()
        for item in inventory:
            if item.get('source_ctx'):
                if item['kind'] == 'layer': source_layers.append(item['source_ctx'])
                else: source_textures.append(item['source_ctx'])
            if item.get('source_name'): source_skills.append(item['source_name'])
            if item.get('target_ctx'):
                targets.add(item['target_ctx'])
                self.assertTrue(item.get('quality_or_ux_benefit'))
        self.assertEqual(len(source_layers), 31)
        self.assertEqual(len(source_textures), 6)
        self.assertEqual(len(source_skills), 5)
        for item in self.pack()['layers'] + self.pack()['textures']:
            self.assertIn(item['ctx'], targets)
        shipped_contexts = {item['ctx'] for item in self.pack()['layers']}
        for item in inventory:
            if item['action'] == 'omitted-pending-approval':
                self.assertNotIn(item['source_ctx'], shipped_contexts)
        self.assertFalse((ASSETS / 'security-candidates.json').exists())



if __name__ == '__main__':
    unittest.main()

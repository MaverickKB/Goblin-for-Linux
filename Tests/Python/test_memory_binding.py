"""Memory targets change at owned turn boundaries and retain target-specific debt."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_release_host import make_host
from goblin_core.config import Config, normalize_settings
from goblin_core.host import GoblinHost
from goblin_core.vendor_aethermind.aethermind import core


class RemoteInspectionTests(unittest.TestCase):
    def test_remote_project_failure_is_visible_in_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            client = Mock()
            client.call.side_effect = [
                {'success': True, 'runtime': {'contract_version': 'adapter-v1'}},
                {'success': False, 'error': 'Selected project does not exist'},
            ]
            try:
                with patch.object(host, '_make_memory_client', return_value=client):
                    report = host.memory_inspect({
                        'mode': 'remote', 'endpoint': 'http://memory.example.test/mcp',
                        'project': 'missing-project',
                    })
                self.assertFalse(report['healthy'])
                self.assertFalse(report['compatible'])
                self.assertEqual(report['issues'][0]['message'], 'Selected project does not exist')
                self.assertNotEqual(host.capture.project_id, 'missing-project')
            finally:
                host.shutdown()


class TurnBackend:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.on_delta = None
        self.calls = 0

    def chat(self, messages, **options):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('Blocked test turn not released')
        return {'content': 'Completed scoped test work.', 'tool_calls': [], 'finish_reason': 'stop'}

    def cancel(self):
        self.release.set()


class MemoryBindingTests(unittest.TestCase):
    def test_completed_status_probe_publishes_ready_layers_without_manual_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                host._memory_state = 'loading'
                host._layer_checked = 0
                host._memory_refreshing = True
                with patch.object(host.capture.client, 'call', return_value={
                    'success': True, 'layer_count': 27,
                }) as status, patch.object(host, '_emit') as emit:
                    host._load_layer_count()
                    event, snapshot = emit.call_args.args
                    self.assertEqual(event, 'snapshot')
                    self.assertEqual(snapshot['memory']['state'], 'ready')
                    self.assertEqual(snapshot['memory']['layers'], 27)
                    self.assertEqual(status.call_count, 1)
                    self.assertFalse(host._memory_refreshing)
            finally:
                host.shutdown()

    def test_active_turn_keeps_capture_and_next_admission_changes_target(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            backend = TurnBackend()
            try:
                values = host.config.settings_dict()
                values['model'] = 'test-model'
                values['model_base_url'] = 'http://127.0.0.1:9/v1'
                host.save_settings(values)
                host.session._backend_provider = lambda: backend
                old = host.capture
                host.submit(host.slug, 'Perform scoped test work.')
                self.assertTrue(backend.entered.wait(3))
                values = host.config.settings_dict()
                core.AetherMind(Path(temp) / 'second-core', create=True)
                values['advanced']['aethermind'] = {'mode': 'embedded', 'local_project': str(Path(temp) / 'second-core')}
                snapshot = host.save_settings(values)
                self.assertIs(host.capture, old)
                self.assertIs(host.session.capture, old)
                self.assertTrue(snapshot['memory']['pending_change'])
                self.assertEqual(snapshot['memory']['project'], old.project_id)
                backend.release.set()
                host._worker.join(5)
                self.assertIs(host.capture, old)
                host.submit(host.slug, 'Continue on the selected core.')
                host._worker.join(5)
                self.assertIsNot(host.capture, old)
                self.assertEqual(host.capture.project_id, str((Path(temp) / 'second-core').resolve()))
                self.assertFalse(host.snapshot()['memory']['pending_change'])
                handlers = host.session.hooks._handlers['turn_start']
                self.assertNotIn(old.turn_start, handlers)
                self.assertIn(host.capture.turn_start, handlers)
            finally:
                backend.release.set()
                host.shutdown()

    def test_target_specific_outboxes_retain_old_debt_and_switch_back_flushes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                original = host.capture
                self.assertEqual(original.outbox_dir, host.session.root / 'outbox')
                with patch.object(original.client, 'write_layer', side_effect=ConnectionError('Scoped injected delivery failure')):
                    original.memory_note('Preserve this old-core debt.')
                    self.assertEqual(len(original.debt()), 1)
                    values = host.config.settings_dict()
                    core.AetherMind(Path(temp) / 'other-core', create=True)
                    values['advanced']['aethermind'] = {'local_project': str(Path(temp) / 'other-core')}
                    host.save_settings(values)
                self.assertNotEqual(host.capture.outbox_dir, original.outbox_dir)
                self.assertEqual(host.capture.debt(), [])
                self.assertEqual(len(original.debt()), 1)
                self.assertEqual(original.debt()[0]['project_id'], original.project_id)
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {}
                host.save_settings(values)
                self.assertEqual(host.capture.outbox_dir, original.outbox_dir)
                self.assertEqual(host.capture.debt(), [])
            finally:
                host.shutdown()

    def test_admitted_worker_keeps_original_core_when_thread_starts_after_switch(self):
        from test_session import FakeBackend
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            entered = threading.Event()
            release = threading.Event()
            original_run = host.session.agents._run
            backend = FakeBackend([])
            host.session._backend_provider = lambda: backend
            pinned = host.capture
            pinned_context = host.session.view.render()

            def delayed_run(*args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Worker startup release was not signalled')
                return original_run(*args)

            try:
                with patch.object(host.session.agents, '_run', delayed_run):
                    started = host.session.agents.spawn('delayed', 'Keep the admitted parent context and core.')
                    self.assertTrue(entered.wait(3))
                    values = host.config.settings_dict()
                    core.AetherMind(Path(temp) / 'new-parent-core', create=True)
                    values['advanced']['aethermind'] = {'local_project': str(Path(temp) / 'new-parent-core')}
                    saved = host.save_settings(values)
                    self.assertEqual(saved['settings_issue'], '')
                    self.assertIsNot(host.capture, pinned)
                    release.set()
                    host.session.agents.wait(5)
                worker = host.session.agents._threads[started['id']]['session']
                self.assertEqual(worker._inherited_context, pinned_context)
                self.assertIs(worker.capture.client, pinned.client)
                self.assertEqual(worker.capture.project_id, pinned.project_id)
                self.assertEqual(worker.capture.personal_project, pinned.personal_project)
                self.assertNotEqual(worker.capture.project_id, host.capture.project_id)
            finally:
                release.set()
                host.shutdown()

    def test_remote_inspection_is_read_only_and_uses_server_project_key(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                old_capture = host.capture
                client = Mock()
                client.call.return_value = {'success': True}
                connection = {'mode': 'remote', 'project': 'server-owned-core',
                              'endpoint': 'https://memory.example.invalid/mcp'}
                with patch.object(host, '_make_memory_client', return_value=client) as factory:
                    report = host.memory_inspect(connection)
                self.assertEqual(report['project_root'], 'server-owned-core')
                self.assertFalse(factory.call_args.args[0].create_project)
                self.assertEqual(client.call.call_args_list[0].args,
                                 ('capabilities', {'project_root': 'server-owned-core'}))
                self.assertEqual(client.call.call_args_list[1].args,
                                 ('status', {'project_root': 'server-owned-core'}))
                self.assertEqual(len(client.method_calls), 2)
                self.assertIs(host.capture, old_capture)
                self.assertEqual(host.config.aethermind.mode, 'embedded')
            finally:
                host.shutdown()

    def test_memory_rpcs_use_background_dispatch_and_exact_arguments(self):
        import goblin_bridge
        bridge = object.__new__(goblin_bridge.Bridge)
        bridge.host = Mock()
        connection = {'mode': 'remote', 'project': 'owned-core'}
        bridge.handle('memory/inspect', {'connection': connection})
        bridge.host.memory_inspect.assert_called_once_with(connection)
        bridge.handle('memory/export', {'destination': '/tmp/requested.zip'})
        bridge.host.memory_export.assert_called_once_with('/tmp/requested.zip')
        bridge.handle('memory/import', {'archive': '/tmp/supplied.zip'})
        bridge.host.memory_import.assert_called_once_with('/tmp/supplied.zip')
        for method in ('memory/inspect', 'memory/export', 'memory/import'):
            self.assertIn(method, goblin_bridge.SLOW_METHODS)

    def test_remote_factory_keeps_server_owned_project_and_disables_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                values = host.config.settings_dict()
                values['advanced']['aethermind'] = {'mode': 'remote', 'project': '/srv/example-core',
                                                   'endpoint': 'https://memory.example.invalid/mcp',
                                                   'token_env': 'TEST_MEMORY_TOKEN'}
                normalized = normalize_settings(values, host.config.manifest, host.home)
                config = Config(host.home, host.config.manifest, normalized)
                with patch('goblin_core.host.AetherMindClient') as factory:
                    client = host._make_memory_client(config.aethermind)
                    self.assertIs(client, factory.return_value)
                    self.assertEqual(factory.call_args.kwargs['url'], 'https://memory.example.invalid/mcp')
                    self.assertFalse(factory.call_args.kwargs['create_project'])
                self.assertEqual(config.aethermind.project, '/srv/example-core')
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()

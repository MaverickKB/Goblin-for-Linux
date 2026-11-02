"""History selection replaces a complete session at one idle admission boundary."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_release_host import make_host
from test_session import BlockingBackend


class HistoryRotationTests(unittest.TestCase):
    def test_rotation_retains_exact_original_and_keeps_core_settings_and_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                original_session = host.session
                original_session.log.append('user', 'Original exact history. 漢字')
                original_path = next((original_session.root / 'log').glob('*.jsonl'))
                before = original_path.read_bytes()
                settings = host.config.settings_dict()
                project = host.capture.project_id
                outbox = host.capture.outbox_dir
                with patch('goblin_core.bootstrap_seeds.ensure_bootstrap', side_effect=AssertionError('History rotation reseeded the core')):
                    rotated = host.history_rotate()
                fresh_id = rotated['history_id']
                self.assertNotEqual(fresh_id, 'legacy')
                self.assertEqual(host.session.log.count(), 0)
                self.assertEqual(host.config.settings_dict(), settings)
                self.assertEqual(host.capture.project_id, project)
                self.assertEqual(host.capture.outbox_dir, outbox)
                self.assertEqual(original_path.read_bytes(), before)
                self.assertEqual(host._manager.path(host.slug), host.session.root)
                host.session.log.append('user', 'Fresh independent history.')
                self.assertTrue(host._retired_histories['legacy']['released'].wait(3))
                selected = host.history_select('legacy')
                self.assertEqual(selected['history_id'], 'legacy')
                self.assertEqual(host.session.log.get(0).text, 'Original exact history. 漢字')
                self.assertEqual(host.history_page()['history_id'], 'legacy')
                self.assertEqual(host.history_record(0)['history_id'], 'legacy')
                self.assertEqual(host.gist_page()['history_id'], 'legacy')
                self.assertEqual({row['id'] for row in host.history_list()['histories']}, {'legacy', fresh_id})
                self.assertTrue(host._retired_histories[fresh_id]['released'].wait(3))
            finally:
                host.shutdown()

    def test_selected_generation_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            selected = host.history_rotate()['history_id']
            host.session.log.append('user', 'Retain the selected generation.')
            self.assertTrue(host._retired_histories['legacy']['released'].wait(3))
            host.shutdown()
            reopened = make_host(Path(temp))
            try:
                self.assertEqual(reopened.snapshot()['history_id'], selected)
                self.assertEqual(reopened.session.log.get(0).text, 'Retain the selected generation.')
            finally:
                reopened.shutdown()

    def test_owned_floor_override_keeps_outgoing_rules_and_applies_incoming_package(self):
        from goblin_core.floor_definition import default_definition, save_definition, load_definition, definition_path
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                outgoing = default_definition()
                outgoing['compose']['world_view'] = 'Outgoing priorities'
                save_definition(host.config, outgoing)
                old_path = definition_path(host.config)
                before = old_path.read_bytes()
                incoming = default_definition()
                incoming['rules'] = [
                    {'id': 'first-example', 'name': 'First example', 'enabled': True, 'signal': 'notice',
                     'when': 'true', 'template': '{message}', 'score': 1.0, 'wake': True},
                    {'id': 'second-example', 'name': 'Second example', 'enabled': True, 'signal': 'notice',
                     'when': 'true', 'template': '{message}', 'score': 1.0, 'wake': True},
                ]
                incoming['rules'].reverse()
                incoming['rules'][0]['enabled'] = False
                incoming['compose']['world_view'] = 'Incoming priorities'
                values = host.config.settings_dict()
                values['advanced']['autonomy'] = {'floor_file': 'autonomy/setup-b/floor.json'}
                with host.idle_maintenance():
                    result = host.save_settings(values, floor_definition=incoming)
                self.assertEqual(result['settings_issue'], '')
                self.assertEqual(old_path.read_bytes(), before)
                stored = load_definition(host.config)
                self.assertEqual(stored['rules'][0]['id'], incoming['rules'][0]['id'])
                self.assertFalse(stored['rules'][0]['enabled'])
                self.assertEqual(stored['compose']['world_view'], 'Incoming priorities')
                self.assertEqual(host.config.world_view, 'Incoming priorities')
            finally:
                host.shutdown()

    def test_stale_history_input_is_rejected_before_durable_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                fresh = host.history_rotate()['history_id']
                with self.assertRaisesRegex(RuntimeError, 'History changed since this input was prepared'):
                    host.submit(host.slug, 'Do not replay into a fresh history.',
                                client_request_id='old-generation-request', expected_history_id='legacy')
                self.assertEqual(host.history_id, fresh)
                self.assertEqual(host.session.log.count(), 0)
                self.assertEqual(host._intake.entries(), [])
                self.assertTrue(host._retired_histories['legacy']['released'].wait(3))
            finally:
                host.shutdown()

    def test_executing_direct_tool_prevents_rotation_without_cancelling_work(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            entered = threading.Event()
            release = threading.Event()
            result = {}

            def hold():
                entered.set()
                release.wait(5)
                return 'Completed unchanged.'

            host.session.tools.register('hold', 'A scoped blocked operation.', {'type': 'object'}, hold)
            worker = threading.Thread(target=lambda: result.setdefault('record', host.execute_tool('hold', {})))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                with patch.object(host.session, 'interrupt', side_effect=AssertionError('Rotation interrupted work')):
                    with self.assertRaisesRegex(RuntimeError, 'Finish current session work'):
                        host.history_rotate()
                self.assertEqual(host.history_id, 'legacy')
                self.assertFalse(release.is_set())
                release.set()
                worker.join(3)
                self.assertTrue(result['record']['success'])
            finally:
                release.set()
                worker.join(3)
                host.shutdown()

    def test_worker_voice_and_autonomy_work_are_idle_preconditions(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            backend = BlockingBackend()
            host.session._backend_provider = lambda: backend
            try:
                host.session.agents.spawn('helper', 'Finish this worker normally.')
                self.assertTrue(backend.entered.wait(3))
                with self.assertRaisesRegex(RuntimeError, 'Finish workers'):
                    host.history_rotate()
                backend.cancelled.set()
                host.session.agents.wait(3)
                host.voice._leases['test-lease'] = {'stopped': False}
                with self.assertRaisesRegex(RuntimeError, 'Finish voice'):
                    host.history_rotate()
                host.voice._leases.clear()
                with patch.object(host.autonomy, 'active_actions', return_value=['approved action still executing']):
                    with self.assertRaisesRegex(RuntimeError, 'Finish autonomy'):
                        host.history_rotate()
                self.assertEqual(host.history_id, 'legacy')
            finally:
                backend.cancelled.set()
                host.voice._leases.clear()
                host.shutdown()

    def test_failed_summary_does_not_prevent_recalling_retained_history(self):
        class FailingSummary:
            def __init__(self):
                self.failed = threading.Event()

            def chat(self, messages, **options):
                self.failed.set()
                raise TimeoutError('An unavailable summary provider')

        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                previous = host.session
                previous.compactor.stop()
                backend = FailingSummary()
                previous.compactor.backend = backend
                previous.compactor.retry_seconds = 60
                previous.compactor.start()
                raw = 'Retain this exact unsummarized source. ' * 80
                previous.log.append('user', raw)
                self.assertTrue(backend.failed.wait(3))
                host.history_rotate()
                self.assertTrue(host._retired_histories['legacy']['released'].wait(3))
                self.assertFalse(previous.tree.is_built(0, 0))
                host.history_select('legacy')
                self.assertEqual(host.session.log.get(0).text, raw)
                self.assertTrue(host.session.compactor.threads)
            finally:
                host.shutdown()

    def test_background_summary_finishes_without_rotation_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            backend = BlockingBackend()
            try:
                original = host.session
                original.compactor.stop()
                original.compactor.backend = backend
                original.compactor.start()
                original.log.append('user', 'Long source requiring a summary. ' * 80)
                self.assertTrue(backend.entered.wait(3))
                with patch.object(backend, 'cancel', side_effect=AssertionError('Active summary was cancelled')):
                    snapshot = host.history_rotate()
                    self.assertNotEqual(snapshot['history_id'], 'legacy')
                    self.assertFalse(backend.cancelled.is_set())
                    self.assertFalse(original._closed)
                backend.cancelled.set()
                self.assertTrue(host._retired_histories['legacy']['released'].wait(3))
                self.assertTrue(original.tree.is_built(0, 0))
            finally:
                backend.cancelled.set()
                host.shutdown()


if __name__ == '__main__':
    unittest.main()

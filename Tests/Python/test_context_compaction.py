"""Mandatory, cancellable context admission and recovery on the real engine path."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core.aethermind import AetherMindClient
from goblin_core.aethermind_direct import DirectMcpClient
from goblin_core.capture import Capture
from goblin_core.compactor import Compactor
from goblin_core.config import default_settings, write_settings
from goblin_core.log import ChatLog
from goblin_core.session import GoblinSession
from goblin_core.tree import SummaryTree
from goblin_core.view import PLACEHOLDER, View


class RoleModel:
    """Deterministic provider fixture, not a live compaction-quality claim."""
    def __init__(self, steps=0):
        self.steps = steps
        self.main_calls = []
        self.summary_calls = []
        self.summary_started = threading.Event()
        self.summary_release = threading.Event()
        self.summary_release.set()
        self.cancel_event = None
        self.on_delta = None
        self.session = None

    def chat(self, messages, tools=None, **options):
        if options.get('tool_choice') == 'none':
            self.summary_started.set()
            while not self.summary_release.wait(0.01):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise RuntimeError('Cancelled provider fixture')
            self.summary_calls.append(copy.deepcopy(messages))
            return {'content': '[user] Preserve the request. [echo] The recorded operation completed.',
                    'finish_reason': 'stop'}
        self.main_calls.append(copy.deepcopy(messages))
        number = len(self.main_calls)
        if number <= self.steps:
            return {'content': '', 'tool_calls': [
                {'id': 'result-' + str(number), 'name': 'large_result', 'arguments': {'index': number}}
            ]}
        return {'content': 'Completed the requested operations.', 'tool_calls': [], 'usage': {}}

    def cancel(self):
        self.summary_release.set()


def session_fixture(home, backend, summary_backend=None):
    manifest = ROOT / 'product.json'
    values = default_settings(json.loads(manifest.read_text()))
    values['model'] = 'unit-provider'
    values['model_base_url'] = 'http://127.0.0.1:1/v1'
    values['workspace'] = str(home / 'workspace')
    config = write_settings(home, manifest, values)
    Path(config.workspace).mkdir(exist_ok=True)
    config.view.high_bytes = 4000
    config.view.low_bytes = 2000
    config.view.node_bytes = 128
    client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
    root = home / 'chat' / config.slug
    capture = Capture(client, root / 'outbox', config.aethermind.project, author='unit-provider')
    session = GoblinSession(lambda: config, lambda: backend, root, capture=capture,
                            approval=lambda name, arguments: True,
                            compactor_backend_factory=(lambda: summary_backend) if summary_backend else None)
    backend.session = session
    return session


class ContextCompactionTests(unittest.TestCase):
    def test_new_long_prompt_does_not_wait_for_its_excluded_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            model = RoleModel()
            summary = RoleModel()
            summary.summary_release.clear()
            session = session_fixture(Path(temp), model, summary)
            session.log.append('user', 'Completed historical request.')
            original = 'Carry out this newly accepted request. ' * 200
            results = []
            errors = []

            def run():
                try:
                    results.append(session.run_turn(original))
                except Exception as error:
                    errors.append(error)

            runner = threading.Thread(target=run)
            runner.start()
            try:
                self.assertTrue(summary.summary_started.wait(2))
                runner.join(2)
                self.assertFalse(runner.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, ['Completed the requested operations.'])
                self.assertFalse(summary.summary_release.is_set())
                self.assertEqual(session.log.get(1).text, original)
                self.assertIn('Completed historical request.', model.main_calls[0][1]['content'])
                self.assertNotIn(original, model.main_calls[0][1]['content'])
                self.assertEqual(model.main_calls[0][-1]['content'], original)
            finally:
                summary.summary_release.set()
                session.interrupt()
                runner.join(3)
                session.close()

    def test_prefix_budget_coarsens_ready_history_without_waiting_for_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=4000, low_bytes=2000)
            log.subscribe(view.on_message)
            try:
                for number in range(6):
                    log.append('user', ('Historical record ' + str(number) + '. ') * 3)
                log.append('user', 'Unfinished suffix. ' * 100)
                for index in range(3):
                    tree.set_node(1, index, 'Pair ' + str(index))
                tree.set_node(2, 0, 'First four records')
                view.high_bytes = 100
                view.low_bytes = 50
                original_frontier = list(view.nodes)
                snapshot = view.ready_snapshot(before=6)
                self.assertIsNotNone(snapshot)
                self.assertLessEqual(len(snapshot['gist'].encode()), view.high_bytes)
                self.assertNotIn(PLACEHOLDER, snapshot['gist'])
                self.assertNotIn('Unfinished suffix', snapshot['gist'])
                self.assertEqual(snapshot['records'], 6)
                self.assertEqual(view.nodes, original_frontier)
                self.assertIsNone(view.ready_snapshot())
            finally:
                log.close()

    def test_prefix_snapshot_splits_a_parent_crossing_the_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=4000, low_bytes=2000)
            log.subscribe(view.on_message)
            try:
                for number in range(4):
                    log.append('user', 'Record ' + str(number))
                tree.set_node(1, 0, 'First two records')
                tree.set_node(1, 1, 'Last two records')
                tree.set_node(2, 0, 'All four records')
                view.nodes = [(2, 0)]
                snapshot = view.ready_snapshot(before=3)
                self.assertEqual(snapshot['records'], 3)
                self.assertIn('First two records', snapshot['gist'])
                self.assertIn('Record 2', snapshot['gist'])
                self.assertNotIn('Last two records', snapshot['gist'])
                self.assertNotIn('All four records', snapshot['gist'])
                self.assertEqual(view.nodes, [(2, 0)])
            finally:
                log.close()

    def test_foreground_waits_for_complete_context_without_owning_the_model_seat(self):
        with tempfile.TemporaryDirectory() as temp:
            model = RoleModel()
            session = session_fixture(Path(temp), model)
            model.summary_release.clear()
            original = 'Keep every word available through history zoom. ' * 80
            session.log.append('user', original)
            self.assertTrue(model.summary_started.wait(2))
            result = {}
            runner = threading.Thread(target=lambda: result.setdefault('reply', session.run_turn('Continue the work')))
            runner.start()
            try:
                time.sleep(0.1)
                self.assertEqual(model.main_calls, [])
                self.assertTrue(runner.is_alive())
                model.summary_release.set()
                runner.join(5)
                self.assertFalse(runner.is_alive())
                self.assertEqual(result['reply'], 'Completed the requested operations.')
                gist = model.main_calls[0][1]['content']
                self.assertNotIn(PLACEHOLDER, gist)
                self.assertNotIn('Continue the work', gist)
                self.assertEqual(session.view.zoom(0, 1, limit=len(original.encode())), original)
            finally:
                model.summary_release.set()
                session.interrupt()
                runner.join(3)
                session.close()

    def test_stop_cancels_context_wait_and_keeps_the_accepted_input(self):
        with tempfile.TemporaryDirectory() as temp:
            model = RoleModel()
            session = session_fixture(Path(temp), model)
            model.summary_release.clear()
            session.log.append('user', 'Unfinished durable source. ' * 100)
            self.assertTrue(model.summary_started.wait(2))
            result = {}
            runner = threading.Thread(target=lambda: result.setdefault('reply', session.run_turn('Retain this request')))
            runner.start()
            try:
                time.sleep(0.1)
                session.interrupt()
                runner.join(3)
                self.assertFalse(runner.is_alive())
                self.assertEqual(result['reply'], 'Interrupted by the user.')
                self.assertEqual(model.main_calls, [])
                self.assertIn('Retain this request', [row.text for row in session.log.iter()])
            finally:
                model.summary_release.set()
                runner.join(3)
                session.close()

    def test_long_tool_turn_finishes_while_compaction_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            model = RoleModel(steps=12)
            summary = RoleModel()
            session = session_fixture(Path(temp), model, summary)
            outputs = []
            result = {}
            errors = []
            summary.summary_release.clear()

            def large_result(index):
                text = ('Recorded result %s. ' % index) * 100
                outputs.append(text)
                if index == 3:
                    self.assertTrue(session.steer('Preserve the final report in English.'))
                return text

            def run(message):
                try:
                    result['reply'] = session.run_turn(message)
                except Exception as error:
                    errors.append(error)

            session.tools.register('large_result', 'Produce a recorded result',
                                   {'type': 'object', 'properties': {'index': {'type': 'integer'}},
                                    'required': ['index']}, large_result)
            runner = threading.Thread(target=run, args=('Complete every operation without repeating one.',))
            runner.start()
            next_runner = None
            try:
                self.assertTrue(summary.summary_started.wait(2))
                runner.join(5)
                self.assertFalse(runner.is_alive(), 'Active turn waited for background compaction')
                self.assertEqual(errors, [])
                self.assertEqual(result['reply'], 'Completed the requested operations.')
                self.assertFalse(summary.summary_release.is_set())
                self.assertEqual(len(outputs), model.steps)
                self.assertEqual(session._context_epoch, 0)
                initial_gist = model.main_calls[0][1]['content']
                for messages in model.main_calls:
                    self.assertEqual(messages[1]['content'], initial_gist)
                for index in range(1, len(model.main_calls)):
                    previous = model.main_calls[index - 1]
                    self.assertEqual(model.main_calls[index][:len(previous)], previous)
                final_messages = model.main_calls[-1]
                for output in outputs:
                    self.assertIn(output, str(final_messages))
                self.assertIn('Preserve the final report in English.', str(final_messages))
                rows = list(session.log.iter())
                steering = [row for row in rows if row.meta.get('steer')]
                self.assertEqual(len(steering), 1)
                echoes = [row for row in rows if row.kind == 'echo']
                self.assertEqual(len(echoes), model.steps)
                for row, output in zip(echoes, outputs):
                    self.assertIn(output, row.text)
                calls_before = len(model.main_calls)
                next_runner = threading.Thread(target=run, args=('Report the completed work.',))
                next_runner.start()
                time.sleep(0.1)
                self.assertTrue(next_runner.is_alive())
                self.assertEqual(len(model.main_calls), calls_before)
                summary.summary_release.set()
                next_runner.join(10)
                self.assertFalse(next_runner.is_alive())
                self.assertEqual(errors, [])
                gist = model.main_calls[-1][1]['content']
                self.assertNotIn(PLACEHOLDER, gist)
                self.assertLessEqual(len(gist.encode()), session.view.high_bytes)
            finally:
                summary.summary_release.set()
                session.interrupt()
                runner.join(3)
                if next_runner is not None:
                    next_runner.join(3)
                session.close()

    def test_workers_build_nodes_while_frontier_merges_are_deferred(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=800, low_bytes=400)
            log.subscribe(view.on_message)
            compactor = Compactor(tree, view, None, '', [], workers=1)
            view.defer_merges()
            compactor.start()
            try:
                for index in range(16):
                    log.append('user', ('Message %s. ' % index) * 12)
                self.assertTrue(compactor.wait_until_summarized(15, 3))
                self.assertTrue(tree.is_built(1, 0))
                self.assertEqual(view.nodes, [(0, index) for index in range(16)])
                view.resume_merges()
                snapshot = compactor.wait_for_context()
                self.assertLessEqual(len(snapshot['gist'].encode()), view.high_bytes)
                self.assertLess(len(view.nodes), 16)
                self.assertEqual(log.count(), 16)
            finally:
                compactor.stop()
                log.close()

    def test_restart_resumes_cancelled_compaction_and_keeps_every_raw_record(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first_model = RoleModel()
            session = session_fixture(home, first_model)
            first_model.summary_release.clear()
            originals = []
            for number in range(20):
                original = ('Durable source %s. ' % number) * 80
                originals.append(original)
                session.log.append('user', original)
            self.assertTrue(first_model.summary_started.wait(2))
            session.close()
            self.assertFalse(session.tree.is_built(0, 0))
            model = RoleModel()
            restored = session_fixture(home, model)
            try:
                self.assertEqual(restored.run_turn('Resume the complete history'),
                                 'Completed the requested operations.')
                gist = model.main_calls[0][1]['content']
                self.assertNotIn(PLACEHOLDER, gist)
                self.assertLessEqual(len(gist.encode()), restored.view.high_bytes)
                for index, original in enumerate(originals):
                    self.assertEqual(restored.log.get(index).text, original)
                    self.assertEqual(restored.view.zoom(index, 1, limit=len(original.encode())), original)
                state = restored.compactor.status()
                self.assertEqual(state['pending'], 0)
                self.assertTrue(state['ready'])
            finally:
                restored.close()

    def test_transient_model_failure_is_retried_without_publishing_a_false_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=4000, low_bytes=2000)
            log.subscribe(view.on_message)

            class RecoveringModel:
                calls = 0

                def chat(self, messages, **options):
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError('Injected provider timeout')
                    return {'content': '[user] The complete source remains available through zoom.',
                            'finish_reason': 'stop'}

            model = RecoveringModel()
            compactor = Compactor(tree, view, model, 'Summarize source data.', [], workers=1)
            compactor.retry_seconds = 0.2
            original = 'A source that requires a real model summary. ' * 50
            log.append('user', original)
            compactor.start()
            try:
                deadline = time.monotonic() + 2
                while not compactor.errors and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(compactor.errors)
                self.assertFalse(tree.is_built(0, 0))
                self.assertTrue(compactor.wait_until_summarized(0, 3))
                self.assertEqual(log.get(0).text, original)
                self.assertGreaterEqual(model.calls, 2)
                self.assertEqual(compactor.status()['failures'], [])
            finally:
                compactor.stop()
                log.close()


if __name__ == '__main__':
    unittest.main()

"""Worker context ownership, report delivery and Stop admission."""
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_session import FakeBackend, make_session
from goblin_core.session import GoblinSession


class WorkerLifecycleTests(unittest.TestCase):
    def test_worker_receives_detached_parent_context_and_parent_zoom(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([])
            parent, _ = make_session(temp, backend)
            original = 'Exact parent source before worker spawn.'
            original_row = parent.log.append('user', original)
            snapshot = parent.view.render()
            try:
                started = parent.agents.spawn('helper', 'Inspect the inherited source.')
                parent.log.append('user', 'Later parent change must not enter the snapshot.')
                parent.agents.wait(5)
                worker = parent.agents._threads[started['id']]['session']
                self.assertEqual(worker._inherited_context, snapshot)
                inherited_wire = json.loads(backend.messages[0][2]['content'])
                self.assertEqual(inherited_wire['goblin_source']['origin'], 'historical_mixed')
                self.assertTrue(inherited_wire['goblin_source']['parent_scope'])
                inherited = inherited_wire['data']
                self.assertIn(snapshot, inherited)
                self.assertIn('source="parent"', inherited)
                self.assertNotIn('Later parent change', inherited)
                self.assertNotIn(original, worker.log.get(0).text)
                self.assertEqual(worker.tools.execute('zoom', {'start': original_row.i, 'count': 1, 'source': 'parent'}), original)
                self.assertEqual(worker.tools.execute('date', {'i': original_row.i, 'source': 'parent'}), parent.view.date(original_row.i))
            finally:
                parent.close()

    def test_automatic_reports_and_explicit_wait_claim_one_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _ = make_session(temp, FakeBackend([]))
            try:
                parent.agents.spawn('automatic', 'Produce an automatic report.')
                parent.agents.wait(5)
                parent._context_records = 0
                messages = []
                parent._drain_work(messages)
                self.assertEqual(len(messages), 1)
                self.assertIn('[automatic]', messages[0]['content'])
                self.assertEqual(parent.agents.wait_for_reports(0)['reports'], [])
                parent._drain_work(messages)
                self.assertEqual(len(messages), 1)

                parent.agents.spawn('explicit', 'Produce an explicit report.')
                reports = parent.agents.wait_for_reports(5)['reports']
                self.assertEqual(len(reports), 1)
                parent._drain_work(messages)
                self.assertEqual(len(messages), 1)

                parent.agents.spawn('covered', 'Produce an already covered report.')
                parent.agents.wait(5)
                parent._context_records = parent.log.count()
                parent._drain_work(messages)
                self.assertEqual(len(messages), 1)
                self.assertEqual(parent.agents.wait_for_reports(0)['reports'], [])
            finally:
                parent.close()

    def test_simultaneous_wait_and_automatic_delivery_claim_report_once(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _ = make_session(temp, FakeBackend([]))
            parent.agents.spawn('racing', 'Produce one shared report.')
            parent.agents.wait(5)
            ready = threading.Barrier(3)
            messages = []
            waited = []

            def automatic():
                ready.wait()
                parent._drain_work(messages)

            def explicit():
                ready.wait()
                waited.extend(parent.agents.wait_for_reports(0)['reports'])

            first = threading.Thread(target=automatic)
            second = threading.Thread(target=explicit)
            first.start()
            second.start()
            try:
                ready.wait()
                first.join(2)
                second.join(2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(len(messages) + len(waited), 1)
                self.assertEqual(parent.agents.claim_reports(), [])
            finally:
                parent.close()

    def test_worker_report_reaches_active_parent_before_its_next_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {'content': '', 'tool_calls': [{'id': 'spawn', 'name': 'delegate', 'arguments': {}}]},
                {'content': 'Worker discovered the exact requested fact.', 'tool_calls': []},
                {'content': 'Parent incorporated the fact.', 'tool_calls': []},
            ])
            parent, _ = make_session(temp, backend)

            def delegate():
                parent.agents.spawn('researcher', 'Find the fact.')
                parent.agents.wait(5)
                return 'Worker thread has finished.'

            parent.tools.register('delegate', 'Delegate work.', {'type': 'object'}, delegate)
            try:
                self.assertEqual(parent.run_turn('Find and incorporate the fact.'), 'Parent incorporated the fact.')
                last_request = backend.messages[-1]
                reports = []
                for message in last_request:
                    content = message.get('content')
                    if isinstance(content, str) and content.startswith('{'):
                        wrapped = json.loads(content)
                        if wrapped.get('data') == '[researcher] Worker discovered the exact requested fact.':
                            self.assertEqual(wrapped['goblin_source']['origin'], 'worker_report')
                            reports.append(message)
                self.assertEqual(len(reports), 1)
                self.assertEqual(parent.agents.wait_for_reports(0)['reports'], [])
            finally:
                parent.close()

    def test_stop_at_worker_admission_stays_cancelled_and_report_is_truthful(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([])
            parent, _ = make_session(temp, backend)
            original_run = GoblinSession.run_turn

            def stopped_run(worker, message, turn_meta=None, **options):
                parent.agents.cancel()
                return original_run(worker, message, turn_meta, **options)

            try:
                with patch.object(GoblinSession, 'run_turn', stopped_run):
                    parent.agents.spawn('stopped', 'This work must not start.')
                    reports = parent.agents.wait_for_reports(5)['reports']
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]['outcome'], 'interrupted')
                self.assertEqual(reports[0]['report'], 'Interrupted by the user.')
                self.assertEqual(backend.messages, [])
                work = parent.log.get(parent.log.count() - 1)
                self.assertEqual(work.kind, 'work')
                self.assertFalse(work.meta['success'])
            finally:
                parent.close()


if __name__ == '__main__':
    unittest.main()

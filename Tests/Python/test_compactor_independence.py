"""Ready summaries remain independent of failures in older leaves."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core.compactor import Compactor
from goblin_core.log import ChatLog
from goblin_core.tree import SummaryTree
from goblin_core.view import PLACEHOLDER, View


class BlockingSummaryModel:
    def __init__(self):
        self.release = threading.Event()
        self.overlapping = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.contexts = []

    def chat(self, messages, **options):
        if 'FAIL-FIRST' in messages[-1]['content']:
            raise TimeoutError('Injected failure of the earliest long leaf')
        with self.lock:
            self.contexts.append(messages[1]['content'])
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active >= 2:
                self.overlapping.set()
        try:
            if not self.release.wait(5):
                raise TimeoutError('Test summary release was not signalled')
            return {'content': '[user] Completed source summary.', 'finish_reason': 'stop'}
        finally:
            with self.lock:
                self.active -= 1

    def cancel(self):
        self.release.set()


class CompactorIndependenceTests(unittest.TestCase):
    def test_context_stops_at_first_unbuilt_node_without_partial_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=4000, low_bytes=2000)
            log.subscribe(view.on_message)
            try:
                log.append('user', 'Complete prefix fact.')
                log.append('user', 'UNBUILT source must stay out. ' * 20)
                log.append('agent', 'Built later text must stay out too.')
                context = view.compaction_context(3, 1000)
                self.assertEqual(context, '<chat>\n0+1|[source:unattributed_input] Complete prefix fact.\n</chat>')
                self.assertNotIn(PLACEHOLDER, context)
                self.assertNotIn('UNBUILT', context)
            finally:
                log.close()

    def test_failed_first_leaf_allows_bounded_parallel_later_leaves_and_parents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=4000, low_bytes=2000)
            log.subscribe(view.on_message)
            log.append('user', 'Complete prefix fact.')
            log.append('user', 'FAIL-FIRST source. ' * 20)
            for index in range(2, 8):
                log.append('user', ('Independent source %s. ' % index) * 20)
            model = BlockingSummaryModel()
            compactor = Compactor(tree, view, model, 'Summarize.', [], workers=3,
                                  concurrent_requests=3)
            compactor.retry_seconds = 60
            compactor.start()
            try:
                self.assertTrue(model.overlapping.wait(3), 'Later leaves stalled behind the failed first leaf')
                self.assertFalse(tree.is_built(1, 0), 'Parent built before its missing child')
                self.assertGreaterEqual(len(model.contexts), 2)
                self.assertEqual(model.contexts[0], model.contexts[1])
                self.assertEqual(model.contexts[0], '<chat>\n0+1|[source:unattributed_input] Complete prefix fact.\n</chat>')
                model.release.set()
                deadline = time.monotonic() + 3
                while not tree.is_built(1, 1) and time.monotonic() < deadline:
                    with tree.changed:
                        tree.changed.wait(0.02)
                self.assertTrue(tree.is_built(0, 2))
                self.assertTrue(tree.is_built(0, 3))
                self.assertTrue(tree.is_built(1, 1))
                self.assertFalse(tree.is_built(0, 1))
                self.assertFalse(tree.is_built(1, 0))
                self.assertLessEqual(model.maximum, 3)
                for context in model.contexts:
                    self.assertNotIn(PLACEHOLDER, context)
                    self.assertNotIn('FAIL-FIRST', context)
            finally:
                model.release.set()
                compactor.stop()
                log.close()


if __name__ == '__main__':
    unittest.main()

"""Lossless cheap merges and pressure-aware scheduling keep the live gist moving."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.compactor import Compactor
from goblin_core.log import ChatLog
from goblin_core.tree import SummaryTree
from goblin_core.view import View


class RecordingModel:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **options):
        self.calls.append(options)
        return {"content": "A compact summary.", "finish_reason": "stop"}


class CompactorLatencyTests(unittest.TestCase):
    def test_a_parent_that_fits_is_copied_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=2000, low_bytes=1000)
            log.subscribe(view.on_message)
            log.append("user", "Keep the supplied facts intact.")
            log.append("agent", "Facts stay intact.\nIncluding this line.")
            model = RecordingModel()
            compactor = Compactor(tree, view, model, "Summarize", [])
            try:
                source = compactor._source(1, 0)
                summary, metadata = compactor._summarize(1, 0)
                self.assertEqual(summary, source)
                self.assertEqual(metadata, {})
                self.assertEqual(model.calls, [])
            finally:
                log.close()

    def test_pressure_prioritizes_ready_merges_and_model_calls_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=2000, low_bytes=1000)
            log.subscribe(view.on_message)
            log.append("user", "First small record.")
            log.append("agent", "Second small record.")
            log.append("user", "A long record. " * 40)
            model = RecordingModel()
            compactor = Compactor(tree, view, model, "Summarize", [])
            try:
                queue, key = compactor._next_job(merge_first=True)
                self.assertEqual(key, (1, 0))
                queue.task_done()
                compactor._summarize(0, 2)
                self.assertEqual(len(model.calls), 1)
                options = model.calls[0]
                self.assertEqual(options["max_tokens"], tree.node_bytes)
                self.assertEqual(options["chat_template_kwargs"], {"enable_thinking": False})
            finally:
                log.close()


if __name__ == "__main__":
    unittest.main()

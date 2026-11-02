"""Cancellation preserves pending gist work and releases the session owner."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.compactor import Compactor
from goblin_core.log import ChatLog
from goblin_core.model_seat import ModelSeat
from goblin_core.session import ModelProxy
from goblin_core.tree import SummaryTree
from goblin_core.view import View


class BlockingModel:
    def __init__(self):
        self.entered = threading.Event()
        self.released = threading.Event()
        self.cancel_event = None
        self.block = True
        self.calls = 0

    def chat(self, messages, tools=None, **options):
        self.calls += 1
        self.entered.set()
        if self.block:
            self.released.wait()
            raise RuntimeError("Cancelled")
        return {"content": "A faithful short summary.", "finish_reason": "stop"}

    def cancel(self):
        self.released.set()


class CompactorShutdownTests(unittest.TestCase):
    def test_stop_cancels_inflight_and_requeues_without_a_false_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=10000, low_bytes=5000)
            log.subscribe(view.on_message)
            model = BlockingModel()
            proxy = ModelProxy(lambda: model, ModelSeat(), model_name="test")
            compactor = Compactor(tree, view, proxy, "Summarize faithfully", [], workers=4)
            source = "This long source must remain recoverable. " * 20
            for _ in range(4):
                log.append("user", source)
            compactor.start()
            self.assertTrue(model.entered.wait(2))
            stopping = threading.Thread(target=compactor.stop, daemon=True)
            stopping.start()
            stopping.join(2)
            try:
                self.assertFalse(stopping.is_alive(), "Stop waited for model completion instead of cancelling it")
                self.assertEqual(model.calls, 1, "Queued workers must not start requests after Stop")
                self.assertEqual(log.get(0).text, source)
                for index in range(4):
                    self.assertFalse(tree.is_built(0, index), "A cancelled model call must not publish an extractive fallback")
                model.block = False
                compactor.start()
                self.assertTrue(compactor.wait_until_summarized(3, 2))
                self.assertEqual(tree.node(0, 0), "A faithful short summary.")
            finally:
                model.released.set()
                stopping.join(2)
                compactor.stop()
                log.close()

    def test_proxy_honors_cancellation_before_starting_a_model_call(self):
        model = BlockingModel()
        proxy = ModelProxy(lambda: model, ModelSeat(), model_name="test")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(RuntimeError):
            proxy.chat([], cancel_event=cancelled)
        self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()

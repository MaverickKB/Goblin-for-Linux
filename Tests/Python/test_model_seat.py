import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.model_seat import ModelSeat, ModelYielded
from goblin_core.session import ModelProxy


class ModelSeatTests(unittest.TestCase):
    def test_preemption_rejects_a_partial_response_even_without_a_transport_error(self):
        seat = ModelSeat()

        class Backend:
            cancel_event = None

            def cancel(self):
                pass

            def chat(self, messages, tools=None, **options):
                seat.cancel(proxy)
                return {"content": "incomplete summary"}

        proxy = ModelProxy(lambda: Backend(), seat, model_name="test")
        with self.assertRaises(ModelYielded):
            proxy.chat([])

    def test_foreground_preempts_background_and_owns_one_seat(self):
        seat = ModelSeat()
        started = threading.Event()
        cancelled = threading.Event()
        finished = threading.Event()
        yielded = []
        background_owner = object()

        def background():
            with seat.use(background_owner, cancelled.set, background=True) as preempted:
                started.set()
                cancelled.wait(2)
                yielded.append(preempted.is_set())
            finished.set()

        worker = threading.Thread(target=background, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        start = time.monotonic()
        with seat.use(object(), lambda: None):
            self.assertTrue(cancelled.is_set())
            self.assertTrue(finished.wait(1))
            self.assertLess(time.monotonic() - start, 1)
        worker.join(2)
        self.assertEqual(yielded, [True])

    def test_cancel_only_reaches_the_current_owner(self):
        seat = ModelSeat()
        owner = object()
        cancelled = threading.Event()
        with seat.use(owner, cancelled.set) as token:
            seat.cancel(object())
            self.assertFalse(cancelled.is_set())
            self.assertFalse(token.is_set())
            seat.cancel(owner)
            self.assertTrue(cancelled.is_set())
            self.assertTrue(token.is_set())


if __name__ == "__main__":
    unittest.main()

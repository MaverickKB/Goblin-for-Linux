"""Compaction requests retain their selected route and cancellation owner."""

from pathlib import Path
from contextlib import contextmanager
import queue
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.model_seat import ModelClientPool, ModelSeat
from goblin_core.session import ModelProxy


class BlockingModel:
    def __init__(self, name):
        self.name = name
        self.seat = ModelSeat()
        self.started = threading.Event()
        self.released = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_event = None
        self.calls = 0

    def chat(self, messages, tools=None, **options):
        self.calls += 1
        self.started.set()
        if not self.released.wait(2):
            raise RuntimeError("Fixture request was not released")
        return {"content": self.name, "tool_calls": []}

    def cancel(self):
        self.cancelled.set()
        self.released.set()


class ModelProxyBindingTests(unittest.TestCase):
    def test_same_route_can_use_two_exclusively_owned_clients(self):
        first = BlockingModel("first")
        second = BlockingModel("second")
        targets = queue.Queue()
        targets.put(first)
        targets.put(second)
        pool = ModelClientPool(lambda binding: targets.get())

        @contextmanager
        def lease(backend, seat, stopped):
            with pool.use("route", 2, stopped) as client:
                yield client, None

        proxy = ModelProxy(lambda: first, model_name="summary", client_lease=lease)
        failures = []

        def request():
            try:
                proxy.chat([])
            except Exception as error:
                failures.append(type(error).__name__)

        threads = []
        try:
            for model in (first, second):
                thread = threading.Thread(target=request)
                threads.append(thread)
                thread.start()
                self.assertTrue(model.started.wait(1))
            proxy.cancel()
            self.assertTrue(first.cancelled.is_set())
            self.assertTrue(second.cancelled.is_set())
        finally:
            first.released.set()
            second.released.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(failures, [])
        self.assertEqual(proxy._active_backends, {})

    def test_route_is_resolved_once_before_choosing_its_seat(self):
        first = BlockingModel("first")
        second = BlockingModel("second")
        selected = {"backend": first}

        def choose_seat(backend):
            selected["backend"] = second
            first.released.set()
            return backend.seat

        proxy = ModelProxy(lambda: selected["backend"], model_name="summary",
                           seat_provider=choose_seat)
        result = proxy.chat([])
        self.assertEqual(result["content"], "first")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)

    def test_cancel_reaches_both_requests_during_a_route_change(self):
        first = BlockingModel("first")
        second = BlockingModel("second")
        targets = queue.Queue()
        targets.put(first)
        targets.put(second)
        proxy = ModelProxy(targets.get, model_name="summary",
                           seat_provider=lambda backend: backend.seat)
        failures = []

        def request():
            try:
                proxy.chat([])
            except Exception as exc:
                failures.append(str(exc))

        threads = []
        try:
            for model in (first, second):
                thread = threading.Thread(target=request)
                threads.append(thread)
                thread.start()
                self.assertTrue(model.started.wait(1))
            proxy.cancel()
            self.assertTrue(first.cancelled.is_set())
            self.assertTrue(second.cancelled.is_set())
        finally:
            first.released.set()
            second.released.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(failures, [])
        self.assertEqual(proxy._active_backends, {})


if __name__ == "__main__":
    unittest.main()

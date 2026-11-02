"""Model clients are exclusive, bounded and cancellation-aware."""
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.model_seat import ModelClientPool, ModelYielded


class ModelClientPoolTests(unittest.TestCase):
    def test_concurrent_leases_use_distinct_clients_and_reuse_idle_clients(self):
        pool = ModelClientPool(lambda binding: object())
        with pool.use("route", 2) as first:
            with pool.use("route", 2) as second:
                self.assertIsNot(first, second)
            with pool.use("route", 2) as reused:
                self.assertIs(reused, second)
        self.assertEqual(pool._active["route"], 0)

    def test_waiting_request_can_stop_without_starting_a_client(self):
        created = []

        def create(binding):
            client = object()
            created.append(client)
            return client

        pool = ModelClientPool(create)
        stopped = threading.Event()
        waiting = threading.Event()
        finished = threading.Event()
        errors = []

        def request():
            waiting.set()
            try:
                with pool.use("route", 1, stopped):
                    errors.append("Unexpected admission")
            except ModelYielded:
                pass
            finally:
                finished.set()

        with pool.use("route", 1):
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(waiting.wait(1))
            stopped.set()
            self.assertTrue(finished.wait(1))
            thread.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(len(created), 1)

    def test_failed_client_construction_releases_capacity(self):
        attempts = []

        def create(binding):
            attempts.append(binding)
            if len(attempts) == 1:
                raise RuntimeError("Construction failed")
            return object()

        pool = ModelClientPool(create)
        with self.assertRaises(RuntimeError):
            with pool.use("route", 1):
                self.fail("Unexpected admission")
        with pool.use("route", 1) as client:
            self.assertIsNotNone(client)
        self.assertEqual(pool._active["route"], 0)


if __name__ == "__main__":
    unittest.main()

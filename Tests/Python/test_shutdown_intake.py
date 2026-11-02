"""Shutdown fences received intake before acknowledging app termination."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from Tests.Python.test_release_bridge import make_bridge, release_settings


class ShutdownIntakeTests(unittest.TestCase):
    def test_shutdown_cancels_preflight_without_changing_operator_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                bridge = make_bridge(Path(directory))
                bridge.host.save_settings(release_settings(Path(directory) / "workspace"))
                entered = threading.Event()
                release = threading.Event()
                model_calls = []

                def preflight():
                    entered.set()
                    release.wait(5)

                def run(text, meta, **kwargs):
                    model_calls.append(text)
                    return "fixture reply"

                bridge.host._register_available_model_key = preflight
                bridge.host.session.run_turn = run
                original_stop = bridge.host.operator_stopped
                try:
                    bridge._enqueue_intake({"id": "send", "method": "chat/send", "params": {
                        "text": "input before quit", "request_id": "quit-fence"}})
                    self.assertTrue(entered.wait(3))
                    result = bridge.handle("shutdown", {})
                    self.assertTrue(result["closed"])
                    release.set()
                    self.assertTrue(bridge._wait_idle(4))
                    self.assertEqual(model_calls, [])
                    self.assertEqual(bridge.host.operator_stopped, original_stop)
                    rows = []
                    for row in bridge.host.session.log.iter():
                        if row.kind == "user" and row.meta.get("request_id") == "quit-fence":
                            rows.append(row)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].text, "input before quit")
                    self.assertTrue(rows[0].meta.get("cancelled"))
                    self.assertIn("shutdown", rows[0].meta["cancel_reason"].lower())
                    pending = json.loads((Path(directory) / "intake/pending.json").read_text())
                    self.assertEqual(pending["entries"], [])
                finally:
                    release.set()
                    bridge.shutdown()


if __name__ == "__main__":
    unittest.main()

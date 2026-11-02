"""Bridge lanes: control stays responsive and intake keeps arrival order.

These tests drive the real bridge reader with temporary homes and synthetic
credentials only.
"""

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

import goblin_bridge
from goblin_core import host as host_module
from goblin_core.config import default_settings


class EmptySecretProvider:
    def model_key(self, endpoint=None):
        return ""


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


def release_settings(workspace):
    values = default_settings(manifest())
    values["workspace"] = str(workspace)
    values["model"] = "release-model"
    values["model_base_url"] = "http://127.0.0.1:9/v1"
    return values


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RecordingStdout:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.lines.append(text)
        return len(text)

    def flush(self):
        return None

    def frames(self):
        parsed = []
        with self.lock:
            lines = list(self.lines)
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                parsed.append(json.loads(text))
            except json.JSONDecodeError:
                continue
        return parsed


def make_bridge(home):
    bridge = goblin_bridge.Bridge(home, ROOT / "product.json")
    bridge.host.secret_provider = EmptySecretProvider()
    return bridge


def run_reader(bridge, requests, stdout):
    payload = "\n".join(json.dumps(request) for request in requests) + "\n"
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    runner = threading.Thread(target=bridge.run, daemon=True)

    def start():
        sys.stdin = io.StringIO(payload)
        sys.stdout = stdout
        runner.start()

    return runner, start, old_stdin, old_stdout


class ControlLaneTests(unittest.TestCase):
    def test_stop_and_approval_answer_while_settings_and_autonomy_block(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = make_bridge(home)
            started = threading.Event()
            release = threading.Event()
            original_save = bridge.host.save_settings
            runner = None
            old_stdin = None
            old_stdout = None

            def slow_save(settings, expected_revision=None, **kwargs):
                started.set()
                release.wait(10)
                return original_save(settings, expected_revision=expected_revision, **kwargs)

            bridge.host.save_settings = slow_save
            approval = host_module.ApprovalRequest("ap-bridge", "write_file", {})
            bridge.host._approvals["ap-bridge"] = approval
            settings = release_settings(home / "workspace")
            settings["agent_name"] = "Slow Save"
            requests = [
                {"id": "save", "method": "settings/save", "params": {"settings": settings}},
                {"id": "auto", "method": "autonomy/set", "params": {"enabled": False}},
                {"id": "approve", "method": "approval/resolve",
                 "params": {"id": "ap-bridge", "accepted": True}},
                {"id": "stop", "method": "chat/stop", "params": {}},
            ]
            stdout = RecordingStdout()
            runner, start, old_stdin, old_stdout = run_reader(bridge, requests, stdout)
            try:
                start()
                self.assertTrue(started.wait(5))
                self.assertTrue(wait_until(
                    lambda: any(frame.get("id") == "stop" for frame in stdout.frames()), 5))
                self.assertTrue(wait_until(
                    lambda: any(frame.get("id") == "approve" for frame in stdout.frames()), 5))
                ids = [frame.get("id") for frame in stdout.frames()]
                self.assertNotIn("save", ids)
                self.assertNotIn("auto", ids)
                self.assertTrue(approval.event.is_set())
                self.assertTrue(approval.accepted)
            finally:
                release.set()
                if runner is not None:
                    runner.join(15)
                sys.stdin = old_stdin
                sys.stdout = old_stdout
                bridge.shutdown()
            self.assertFalse(runner.is_alive())
            ids = [frame.get("id") for frame in stdout.frames()]
            self.assertIn("save", ids)
            self.assertIn("auto", ids)


class IntakeLaneTests(unittest.TestCase):
    def test_arrival_order_and_stop_fence_survive_slow_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = make_bridge(home)
            entered = threading.Event()
            release = threading.Event()
            blocked_once = []
            run_calls = []
            runner = None
            old_stdin = None
            old_stdout = None

            def slow_register():
                if not blocked_once:
                    blocked_once.append(True)
                    entered.set()
                    release.wait(10)
                return ""

            def fake_run(text, meta, **kwargs):
                run_calls.append(text)
                return "reply"

            bridge.host._register_available_model_key = slow_register
            bridge.host.session.run_turn = fake_run
            with redirect_stdout(io.StringIO()):
                bridge.host.save_settings(release_settings(home / "workspace"))
            requests = [
                {"id": "a", "method": "chat/send",
                 "params": {"text": "first arrived", "request_id": "turn-a"}},
                {"id": "b", "method": "chat/queue",
                 "params": {"text": "second arrived", "request_id": "turn-b"}},
                {"id": "stop", "method": "chat/stop", "params": {}},
            ]
            stdout = RecordingStdout()
            runner, start, old_stdin, old_stdout = run_reader(bridge, requests, stdout)
            try:
                start()
                self.assertTrue(entered.wait(5))
                self.assertTrue(wait_until(
                    lambda: any(frame.get("id") == "stop" for frame in stdout.frames()), 5))
                self.assertFalse(any(frame.get("id") == "a" for frame in stdout.frames()))
                release.set()
                runner.join(15)
                self.assertFalse(runner.is_alive())
                ids = [frame.get("id") for frame in stdout.frames()]
                self.assertLess(ids.index("stop"), ids.index("a"))
                self.assertLess(ids.index("a"), ids.index("b"))
                self.assertEqual(run_calls, [])
                rows = []
                for message in bridge.host.session.log.iter():
                    if message.kind == "user" and message.meta.get("source") == "chat":
                        rows.append(message)
                self.assertEqual([row.text for row in rows], ["first arrived", "second arrived"])
                self.assertEqual([row.meta.get("request_id") for row in rows], ["turn-a", "turn-b"])
                for row in rows:
                    self.assertTrue(row.meta.get("cancelled"))
                journal = home / "intake" / "pending.json"
                if journal.exists():
                    payload = json.loads(journal.read_text(encoding="utf-8"))
                    self.assertEqual(payload.get("entries") or [], [])
            finally:
                release.set()
                if runner is not None:
                    runner.join(15)
                sys.stdin = old_stdin
                sys.stdout = old_stdout
                bridge.shutdown()


class BridgeSettingsTests(unittest.TestCase):
    def test_autonomy_expected_revision_is_forwarded_and_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = make_bridge(home)
            try:
                with redirect_stdout(io.StringIO()):
                    first = bridge.host.save_settings(release_settings(home / "workspace"))
                    self.assertEqual(first["settings_revision"], 1)
                    with self.assertRaises(RuntimeError):
                        bridge.handle("autonomy/set", {"enabled": True, "expected_revision": 0})
                    enabled = bridge.handle("autonomy/set",
                                            {"enabled": True, "expected_revision": 1})
                    self.assertEqual(enabled["settings_revision"], 2)
                    disabled = bridge.handle("autonomy/set", {"enabled": False})
                    self.assertEqual(disabled["settings_revision"], 3)
                    self.assertFalse(disabled["settings"]["advanced"]["autonomy"]["enabled"])
            finally:
                bridge.shutdown()

    def test_settings_apply_failure_returns_a_snapshot_not_an_error_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = make_bridge(home)
            try:
                with redirect_stdout(io.StringIO()):
                    first = bridge.host.save_settings(release_settings(home / "workspace"))

                def broken_apply(config):
                    raise RuntimeError("plugin reload failed")

                bridge.host.session.apply_config = broken_apply
                changed = release_settings(home / "workspace")
                changed["agent_name"] = "Committed Name"
                stdout = RecordingStdout()
                old_stdout = sys.stdout
                try:
                    sys.stdout = stdout
                    bridge.dispatch({"id": "save", "method": "settings/save",
                                     "params": {"settings": changed,
                                                "expected_revision": first["settings_revision"]}})
                finally:
                    sys.stdout = old_stdout
                frames = [frame for frame in stdout.frames() if frame.get("id") == "save"]
                self.assertEqual(len(frames), 1)
                self.assertIn("result", frames[0])
                result = frames[0]["result"]
                self.assertEqual(result["settings"]["agent_name"], "Committed Name")
                self.assertEqual(result["settings_revision"], 2)
                self.assertFalse(result["ready"])
                self.assertIn("session apply failed", result["settings_issue"])
            finally:
                bridge.shutdown()


if __name__ == "__main__":
    unittest.main()

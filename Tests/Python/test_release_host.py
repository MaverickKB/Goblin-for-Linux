"""Release-boundary tests: workspace adoption, readiness, queue Stop and settings.

These tests drive the real host and the real bridge reader. They use isolated
temporary homes and never touch the installed app's home or Keychain.
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
from goblin_core.config import ConfigError, default_settings, write_settings
from goblin_core.host import GoblinHost


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


def release_settings(workspace, model="release-model", endpoint="http://127.0.0.1:9/v1"):
    values = default_settings(manifest())
    values["workspace"] = str(workspace)
    values["model"] = model
    values["model_base_url"] = endpoint
    return values


class EmptySecretProvider:
    def model_key(self, endpoint=None):
        return ""


def make_host(home, emit=None):
    host = GoblinHost(home, ROOT / "product.json", emit=emit)
    # Tests never read the real Keychain.
    host.secret_provider = EmptySecretProvider()
    return host


def user_rows(host):
    rows = []
    for message in host.session.log.iter():
        if message.kind == "user":
            rows.append(message)
    return rows


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


class WorkspaceAdoptionTests(unittest.TestCase):
    def test_save_rejects_bad_workspace_without_poisoning_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            try:
                good = release_settings(home / "workspace")
                host.save_settings(good)
                saved_workspace = host.config.workspace
                blocked = home / "blocked"
                blocked.write_text("not a folder", encoding="utf-8")

                bad = release_settings(blocked)
                with self.assertRaises(ConfigError) as raised:
                    host.save_settings(bad)
                self.assertIn("Workspace path is a file", str(raised.exception))
                self.assertEqual(host.config.workspace, saved_workspace)
                on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(on_disk["workspace"], saved_workspace)
                self.assertEqual(host.snapshot()["workspace"], saved_workspace)
            finally:
                host.shutdown()

            again = make_host(home)
            try:
                self.assertEqual(again.config.workspace, saved_workspace)
                self.assertTrue(again.snapshot()["ready"])
            finally:
                again.shutdown()

    def test_write_settings_creates_missing_workspace_before_config(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            workspace = home / "new" / "space"
            values = release_settings(workspace)
            write_settings(home, ROOT / "product.json", values)
            self.assertTrue(workspace.is_dir())
            self.assertTrue((home / "config.json").exists())

    def test_write_settings_refuses_unwritable_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            readonly = home / "readonly"
            readonly.mkdir()
            readonly.chmod(0o500)
            try:
                values = default_settings(manifest())
                values["workspace"] = str(readonly)
                with self.assertRaises(ConfigError) as raised:
                    write_settings(home, ROOT / "product.json", values)
                self.assertIn("Workspace", str(raised.exception))
                self.assertFalse((home / "config.json").exists())
            finally:
                readonly.chmod(0o700)

    def test_startup_missing_workspace_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            workspace = home / "removed"
            values = release_settings(workspace)
            (home / "config.json").write_text(json.dumps(values), encoding="utf-8")
            host = make_host(home)
            try:
                self.assertTrue(workspace.is_dir())
                self.assertTrue(host.snapshot()["ready"])
            finally:
                host.shutdown()

    def test_startup_unwritable_workspace_stays_live(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            readonly = home / "readonly"
            readonly.mkdir()
            readonly.chmod(0o500)
            try:
                values = release_settings(readonly)
                (home / "config.json").write_text(json.dumps(values), encoding="utf-8")
                host = make_host(home)
                try:
                    snapshot = host.snapshot()
                    self.assertFalse(snapshot["ready"])
                    self.assertIn("Workspace is not writable", snapshot["setup_issue"])
                    with self.assertRaises(RuntimeError):
                        host.submit(host.slug, "cannot run")
                    self.assertEqual(user_rows(host), [])
                    readonly.chmod(0o700)
                    repaired = host.save_settings(release_settings(readonly))
                    self.assertTrue(repaired["ready"])
                finally:
                    host.shutdown()
            finally:
                if readonly.exists():
                    readonly.chmod(0o700)

    def test_startup_bad_workspace_keeps_repair_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            blocked = home / "blocked"
            blocked.write_text("not a folder", encoding="utf-8")
            values = release_settings(home / "unused")
            values["workspace"] = str(blocked)
            (home / "config.json").write_text(json.dumps(values), encoding="utf-8")

            host = make_host(home)
            try:
                snapshot = host.snapshot()
                self.assertFalse(snapshot["ready"])
                self.assertIn("Workspace path is a file", snapshot["setup_issue"])
                self.assertEqual(snapshot["workspace"], str(blocked.resolve()))
                with self.assertRaises(RuntimeError) as raised:
                    host.submit(host.slug, "this cannot run")
                self.assertIn("Workspace", str(raised.exception))
                self.assertEqual(user_rows(host), [])

                repaired = host.save_settings(release_settings(home / "workspace"))
                self.assertTrue(repaired["ready"])
                self.assertEqual(repaired["setup_issue"], "")
            finally:
                host.shutdown()

            again = make_host(home)
            try:
                self.assertTrue(again.snapshot()["ready"])
                self.assertEqual(again.snapshot()["setup_issue"], "")
            finally:
                again.shutdown()


class ReadinessTests(unittest.TestCase):
    def test_blank_model_is_not_ready_and_rejects_before_logging(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            try:
                snapshot = host.snapshot()
                self.assertFalse(snapshot["ready"])
                self.assertIn("Model is not configured", snapshot["setup_issue"])
                self.assertEqual(snapshot["active_turn_id"], "")
                self.assertEqual(snapshot["turns"], [])
                self.assertEqual(snapshot["settings_revision"], 0)
                with self.assertRaises(RuntimeError):
                    host.submit(host.slug, "hello")
                self.assertEqual(user_rows(host), [])

                record = host.execute_tool("agent_backends", {})
                self.assertTrue(record["success"])

                saved = host.save_settings(release_settings(home / "workspace"))
                self.assertTrue(saved["ready"])
                self.assertEqual(saved["setup_issue"], "")
            finally:
                host.shutdown()

    def test_queued_turn_keeps_text_as_canceled_row_after_setup_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            try:
                host.save_settings(release_settings(home / "workspace"))
                started = threading.Event()
                release = threading.Event()

                def fake_run(text, meta, **kwargs):
                    request_id = meta.get("request_id") or "turn-first"
                    host.session.log.append("user", text, dict(meta, request_id=request_id))
                    started.set()
                    release.wait(5)
                    return "first reply"

                host.session.run_turn = fake_run
                host.submit(host.slug, "first", client_request_id="turn-first")
                self.assertTrue(started.wait(5))
                host.submit(host.slug, "second", client_request_id="turn-second")
                broken = release_settings(home / "workspace")
                broken["model"] = ""
                host.save_settings(broken)
                release.set()
                self.assertTrue(wait_until(lambda: not host.busy()))
                snapshot = host.snapshot()
                states = {}
                for item in snapshot["turns"]:
                    states[item["id"]] = item["state"]
                self.assertEqual(states.get("turn-second"), "error")
                rows = []
                for message in user_rows(host):
                    if message.meta.get("request_id") == "turn-second":
                        rows.append(message)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].text, "second")
                self.assertTrue(rows[0].meta.get("cancelled"))
                self.assertIn("Setup needed", rows[0].meta.get("cancel_reason"))
            finally:
                host.shutdown()


class StopQueueTests(unittest.TestCase):
    def test_stop_preserves_cancelled_queued_text_and_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            events = []
            host = make_host(home, emit=lambda event, data: events.append((event, data)))
            try:
                host.save_settings(release_settings(home / "workspace"))
                started = threading.Event()

                def fake_run(text, meta, **kwargs):
                    request_id = meta.get("request_id") or "turn-a"
                    host.session.log.append("user", text, dict(meta, request_id=request_id))
                    started.set()
                    host.session._interrupt.wait(5)
                    if host.session._interrupt.is_set():
                        host.session.last_turn_interrupted = True
                    return "partial"

                host.session.run_turn = fake_run
                host.submit(host.slug, "first turn", client_request_id="turn-a")
                self.assertTrue(started.wait(5))
                host.submit(host.slug, "queued message", client_request_id="turn-b")

                active_snapshot = host.snapshot()
                self.assertEqual(active_snapshot["active_turn_id"], "turn-a")
                turn_starts = [data for event, data in events
                               if event == "activity" and data.get("phase") == "turn_start"]
                self.assertTrue(turn_starts)
                self.assertEqual(turn_starts[-1]["active_turn_id"], "turn-a")
                states = {}
                for item in active_snapshot["turns"]:
                    states[item["id"]] = item["state"]
                self.assertEqual(states.get("turn-a"), "running")
                self.assertEqual(states.get("turn-b"), "queued")

                host._stream("text", "chunk")
                stream_events = [data for event, data in events if event == "stream"]
                self.assertTrue(stream_events)
                self.assertEqual(stream_events[-1]["active_turn_id"], "turn-a")

                result = host.interrupt()
                self.assertEqual(result["active_turn_id"], "turn-a")
                self.assertEqual(result["cancelled_turn_ids"], ["turn-b"])
                self.assertTrue(wait_until(lambda: not host.busy()))

                snapshot = host.snapshot()
                states = {}
                for item in snapshot["turns"]:
                    states[item["id"]] = item["state"]
                self.assertEqual(states.get("turn-b"), "cancelled")
                self.assertEqual(snapshot["active_turn_id"], "")
                rows = []
                for row in host.history_page()["messages"]:
                    if row["kind"] == "user" and row["text"] == "queued message":
                        rows.append(row)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["turn_id"], "turn-b")
                self.assertEqual(rows[0]["status"], "cancelled")
            finally:
                host.shutdown()

            again = make_host(home)
            try:
                rows = []
                for row in again.history_page()["messages"]:
                    if row["kind"] == "user" and row["text"] == "queued message":
                        rows.append(row)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["turn_id"], "turn-b")
                self.assertEqual(rows[0]["status"], "cancelled")
            finally:
                again.shutdown()


class SettingsRevisionTests(unittest.TestCase):
    def test_expected_revision_blocks_stale_form(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            try:
                first = release_settings(home / "workspace")
                saved = host.save_settings(first)
                self.assertEqual(saved["settings_revision"], 1)

                stale = release_settings(home / "workspace")
                stale["agent_name"] = "Stale Name"
                with self.assertRaises(RuntimeError):
                    host.save_settings(stale, expected_revision=0)
                self.assertNotEqual(host.config.agent_name, "Stale Name")

                fresh = release_settings(home / "workspace")
                fresh["agent_name"] = "Fresh Name"
                saved = host.save_settings(fresh, expected_revision=1)
                self.assertEqual(saved["settings_revision"], 2)
                self.assertEqual(host.config.agent_name, "Fresh Name")

                with self.assertRaises(ValueError):
                    host.save_settings(fresh, expected_revision=True)

                host.autonomy.start = lambda: None
                host.autonomy.close = lambda: None
                host.set_autonomy(True)
                self.assertEqual(host.snapshot()["settings_revision"], 3)
                host.set_autonomy(False)
                self.assertEqual(host.snapshot()["settings_revision"], 4)
            finally:
                host.shutdown()


class BridgeSettingsLaneTests(unittest.TestCase):
    def test_expected_revision_is_forwarded_by_the_bridge(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = goblin_bridge.Bridge(home, ROOT / "product.json")
            try:
                values = release_settings(home / "workspace")
                with redirect_stdout(io.StringIO()):
                    bridge.handle("settings/save", {"settings": values})
                    with self.assertRaises(RuntimeError):
                        bridge.handle("settings/save", {"settings": values,
                                                        "expected_revision": 0})
            finally:
                bridge.shutdown()

    def test_settings_save_does_not_block_stop_intake(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            bridge = goblin_bridge.Bridge(home, ROOT / "product.json")
            started = threading.Event()
            release = threading.Event()
            original_save = bridge.host.save_settings

            def slow_save(settings, expected_revision=None, **kwargs):
                started.set()
                release.wait(10)
                return original_save(settings, expected_revision=expected_revision, **kwargs)

            bridge.host.save_settings = slow_save
            settings = release_settings(home / "workspace")
            requests = [
                json.dumps({"id": "slow-save", "method": "settings/save",
                            "params": {"settings": settings}}),
                json.dumps({"id": "stop", "method": "chat/stop", "params": {}}),
            ]
            stdin = io.StringIO("\n".join(requests) + "\n")
            stdout = RecordingStdout()
            old_stdin = sys.stdin
            old_stdout = sys.stdout
            runner = threading.Thread(target=bridge.run, daemon=True)
            try:
                sys.stdin = stdin
                sys.stdout = stdout
                runner.start()
                self.assertTrue(started.wait(5))
                self.assertTrue(wait_until(
                    lambda: any(frame.get("id") == "stop" for frame in stdout.frames()), 5))
                self.assertFalse(any(frame.get("id") == "slow-save" for frame in stdout.frames()))
            finally:
                release.set()
                runner.join(15)
                sys.stdin = old_stdin
                sys.stdout = old_stdout
                bridge.shutdown()
            self.assertFalse(runner.is_alive())
            self.assertTrue(any(frame.get("id") == "slow-save" for frame in stdout.frames()))


if __name__ == "__main__":
    unittest.main()

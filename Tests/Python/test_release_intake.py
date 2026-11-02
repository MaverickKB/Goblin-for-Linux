"""Host intake durability: setup loss, Stop fences, abrupt exit and settings.

Every fixture uses a temporary home and a synthetic secret provider. No real
Keychain item, model route or installed app home is touched.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import ConfigError, default_settings
from goblin_core.host import GoblinHost


class EmptySecretProvider:
    def model_key(self, endpoint=None):
        return ""


class FixedSecretProvider:
    def __init__(self, key):
        self.key = key
        self.endpoints = []

    def model_key(self, endpoint=None):
        self.endpoints.append(endpoint)
        return self.key


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


def release_settings(workspace, model="release-model", endpoint="http://127.0.0.1:9/v1"):
    values = default_settings(manifest())
    values["workspace"] = str(workspace)
    values["model"] = model
    values["model_base_url"] = endpoint
    return values


def make_host(home, provider=None):
    host = GoblinHost(home, ROOT / "product.json", emit=lambda event, data: None)
    host.secret_provider = provider or EmptySecretProvider()
    return host


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def user_rows(host):
    rows = []
    for message in host.session.log.iter():
        if message.kind == "user":
            rows.append(message)
    return rows


def journal_entries(home):
    path = Path(home) / "intake" / "pending.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("entries") or []


def log_text(home, slug):
    directory = Path(home) / "chat" / slug / "log"
    text = ""
    for path in sorted(directory.glob("*.jsonl")):
        text += path.read_text(encoding="utf-8")
    return text


class StopPreflightTests(unittest.TestCase):
    def test_stop_during_submit_preflight_persists_canceled_input(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            entered = threading.Event()
            release = threading.Event()

            def slow_register():
                entered.set()
                release.wait(5)
                return ""

            host._register_available_model_key = slow_register
            holder = {}
            try:
                host.save_settings(release_settings(home / "workspace"))

                def submit():
                    holder["turn_id"] = host.submit(
                        host.slug, "arrived before stop", client_request_id="fenced-submit",
                        expected_stop_generation=0)

                thread = threading.Thread(target=submit)
                thread.start()
                self.assertTrue(entered.wait(5))
                host.interrupt()
                release.set()
                thread.join(10)
                self.assertFalse(thread.is_alive())
                turn_id = holder["turn_id"]
                self.assertEqual(host.turn(host.slug, turn_id)["state"], "cancelled")
                rows = [message for message in user_rows(host)
                        if message.meta.get("request_id") == "fenced-submit"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].text, "arrived before stop")
                self.assertTrue(rows[0].meta.get("cancelled"))
                self.assertEqual(journal_entries(home), [])
            finally:
                release.set()
                host.shutdown()


class FailureDurabilityTests(unittest.TestCase):
    def test_failed_turn_keeps_accepted_text(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)

            def failed_run(text, meta, **kwargs):
                raise RuntimeError("backend refused before admission")

            host.session.run_turn = failed_run
            try:
                host.save_settings(release_settings(home / "workspace"))
                turn_id = host.submit(host.slug, "text that must survive",
                                      client_request_id="failure-a")
                self.assertTrue(wait_until(lambda: not host.busy()))
                state = host.turn(host.slug, turn_id)
                self.assertEqual(state["state"], "error")
                self.assertIn("backend refused", state["error"])
                rows = [message for message in user_rows(host)
                        if message.meta.get("request_id") == "failure-a"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].text, "text that must survive")
                self.assertTrue(rows[0].meta.get("cancelled"))
                self.assertEqual(journal_entries(home), [])
            finally:
                host.shutdown()


class StopFenceTests(unittest.TestCase):
    def test_human_message_after_stop_clears_the_fence_and_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            run_calls = []

            def fake_run(text, meta, **kwargs):
                request_id = meta.get("request_id") or "after-stop"
                host.session.log.append("user", text, dict(meta, request_id=request_id))
                run_calls.append(text)
                return "reply"

            host.session.run_turn = fake_run
            try:
                host.save_settings(release_settings(home / "workspace"))
                host.interrupt()
                self.assertTrue(host.operator_stopped)
                with self.assertRaises(RuntimeError):
                    host.submit(host.slug, "autonomous wake", source="autonomy")
                self.assertEqual(journal_entries(home), [])
                turn_id = host.submit(host.slug, "continue", client_request_id="after-stop")
                self.assertFalse(host.operator_stopped)
                self.assertTrue(wait_until(lambda: not host.busy()))
                self.assertEqual(host.turn(host.slug, turn_id)["state"], "done")
                self.assertEqual(run_calls, ["continue"])
            finally:
                host.shutdown()


class JournalRecoveryTests(unittest.TestCase):
    def test_abrupt_exit_recovers_canceled_row_with_attachments_once(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            gate = threading.Event()
            host._work = lambda: gate.wait(5)
            note = home / "note.txt"
            note.write_text("attachment proof", encoding="utf-8")
            try:
                host.save_settings(release_settings(home / "workspace"))
                refs = host._ingest_attachments([str(note)])
                host.submit(host.slug, "queued with attachment", client_request_id="crash-a",
                            attachments=refs)
                entries = journal_entries(home)
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["id"], "crash-a")
                self.assertEqual(entries[0]["text"], "queued with attachment")
                self.assertEqual(entries[0]["meta"]["attachments"][0]["id"], refs[0]["id"])

                # Simulate abrupt process exit: release the log lock without a
                # host shutdown, then launch a new host against the same home.
                host.session.log.close()
                gate.set()
                again = make_host(home)
                try:
                    rows = [message for message in user_rows(again)
                            if message.meta.get("request_id") == "crash-a"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].text, "queued with attachment")
                    self.assertTrue(rows[0].meta.get("cancelled"))
                    self.assertEqual(rows[0].meta.get("attachments")[0]["id"], refs[0]["id"])
                    self.assertEqual(journal_entries(home), [])
                    self.assertEqual(again.turn(again.slug, "crash-a")["state"], "cancelled")
                finally:
                    again.shutdown()

                third = make_host(home)
                try:
                    rows = [message for message in user_rows(third)
                            if message.meta.get("request_id") == "crash-a"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(journal_entries(home), [])
                finally:
                    third.shutdown()
            finally:
                gate.set()

    def test_recovery_skips_entry_when_raw_row_is_already_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            gate = threading.Event()
            host._work = lambda: gate.wait(5)
            try:
                host.save_settings(release_settings(home / "workspace"))
                host.submit(host.slug, "already committed", client_request_id="crash-b")
                self.assertEqual(len(journal_entries(home)), 1)
                host.session.log.append("user", "already committed",
                                        {"request_id": "crash-b", "source": "chat"})
                host.session.log.close()
                gate.set()
                again = make_host(home)
                try:
                    rows = [message for message in user_rows(again)
                            if message.meta.get("request_id") == "crash-b"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(journal_entries(home), [])
                finally:
                    again.shutdown()
            finally:
                gate.set()

    def test_repeated_identity_after_restart_does_not_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)

            def fake_run(text, meta, **kwargs):
                request_id = meta.get("request_id") or "repeat-a"
                host.session.log.append("user", text, dict(meta, request_id=request_id))
                return "first reply"

            host.session.run_turn = fake_run
            try:
                host.save_settings(release_settings(home / "workspace"))
                first_id = host.submit(host.slug, "only once", client_request_id="repeat-a")
                self.assertTrue(wait_until(lambda: not host.busy()))
                self.assertEqual(host.turn(host.slug, first_id)["state"], "done")
            finally:
                host.shutdown()

            again = make_host(home)
            runs = []

            def fail_if_replayed(text, meta, **kwargs):
                runs.append(text)
                return "replayed"

            again.session.run_turn = fail_if_replayed
            try:
                repeated = again.submit(again.slug, "only once", client_request_id="repeat-a")
                self.assertEqual(repeated, "repeat-a")
                self.assertEqual(runs, [])
                self.assertEqual(again.turn(again.slug, "repeat-a")["state"], "done")
                rows = [message for message in user_rows(again)
                        if message.meta.get("request_id") == "repeat-a"]
                self.assertEqual(len(rows), 1)
            finally:
                again.shutdown()


class RedactionTests(unittest.TestCase):
    def test_journal_and_canceled_row_redact_endpoint_key(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            key = "synthetic-intake-secret"
            provider = FixedSecretProvider(key)
            host = make_host(home, provider)
            gate = threading.Event()
            host._work = lambda: gate.wait(5)
            try:
                host.save_settings(release_settings(home / "workspace"))
                host.submit(host.slug, "my key is " + key, client_request_id="redact-a")
                entries = journal_entries(home)
                self.assertEqual(len(entries), 1)
                self.assertNotIn(key, json.dumps(entries))
                self.assertIn("[REDACTED]", entries[0]["text"])
                self.assertEqual(provider.endpoints, ["http://127.0.0.1:9/v1"])

                host.interrupt()
                self.assertTrue(wait_until(lambda: not host.busy()))
                text = log_text(home, host.slug)
                self.assertNotIn(key, text)
                self.assertIn("[REDACTED]", text)
                rows = [message for message in user_rows(host)
                        if message.meta.get("request_id") == "redact-a"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(journal_entries(home), [])
            finally:
                gate.set()
                host.shutdown()


class ShutdownDurabilityTests(unittest.TestCase):
    def test_shutdown_during_key_preflight_recovers_text_next_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            entered = threading.Event()
            release = threading.Event()

            def slow_register():
                entered.set()
                release.wait(5)
                return ""

            host._register_available_model_key = slow_register
            holder = {}

            def submit():
                try:
                    holder["turn_id"] = host.submit(
                        host.slug, "preflight at shutdown",
                        client_request_id="shutdown-preflight")
                except Exception as error:
                    holder["error"] = error

            thread = threading.Thread(target=submit)
            try:
                host.save_settings(release_settings(home / "workspace"))
                thread.start()
                self.assertTrue(entered.wait(5))
            finally:
                try:
                    host.shutdown()
                finally:
                    release.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", holder, holder.get("error"))

            again = make_host(home)
            try:
                rows = [message for message in user_rows(again)
                        if message.meta.get("request_id") == "shutdown-preflight"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].text, "preflight at shutdown")
                self.assertTrue(rows[0].meta.get("cancelled"))
                self.assertEqual(again.turn(again.slug, "shutdown-preflight")["state"], "cancelled")
                self.assertEqual(journal_entries(home), [])
            finally:
                again.shutdown()

    def test_normal_shutdown_keeps_active_and_queued_text(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            started = threading.Event()

            def fake_run(text, meta, **kwargs):
                request_id = meta.get("request_id") or "shutdown-active"
                host.session.log.append("user", text, dict(meta, request_id=request_id))
                started.set()
                host.session._interrupt.wait(5)
                if host.session._interrupt.is_set():
                    host.session.last_turn_interrupted = True
                return "Interrupted by the user."

            host.session.run_turn = fake_run
            try:
                host.save_settings(release_settings(home / "workspace"))
                host.submit(host.slug, "active at shutdown", client_request_id="shutdown-active")
                self.assertTrue(started.wait(5))
                host.submit(host.slug, "queued at shutdown", client_request_id="shutdown-queued")
            finally:
                host.shutdown()

            again = make_host(home)
            try:
                rows = user_rows(again)
                active_rows = [row for row in rows if row.meta.get("request_id") == "shutdown-active"]
                queued_rows = [row for row in rows if row.meta.get("request_id") == "shutdown-queued"]
                self.assertEqual(len(active_rows), 1)
                self.assertEqual(len(queued_rows), 1)
                self.assertEqual(queued_rows[0].text, "queued at shutdown")
                self.assertTrue(queued_rows[0].meta.get("cancelled"))
                self.assertEqual(journal_entries(home), [])
            finally:
                again.shutdown()


class SettingsApplyTests(unittest.TestCase):
    def test_apply_failure_returns_committed_snapshot_and_retry_clears_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            try:
                first = host.save_settings(release_settings(home / "workspace"))
                self.assertEqual(first["settings_revision"], 1)
                self.assertTrue(first["ready"])
                original_apply = host.session.apply_config

                def broken_apply(config):
                    raise RuntimeError("plugin reload failed")

                host.session.apply_config = broken_apply
                changed = release_settings(home / "workspace")
                changed["agent_name"] = "Applied Despite Error"
                result = host.save_settings(changed, expected_revision=1)
                self.assertEqual(result["settings_revision"], 2)
                self.assertEqual(result["settings"]["agent_name"], "Applied Despite Error")
                self.assertFalse(result["ready"])
                self.assertIn("session apply failed", result["settings_issue"])
                self.assertIn("RuntimeError: plugin reload failed", result["settings_issue"])
                self.assertEqual(host.config.agent_name, "Applied Despite Error")
                on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(on_disk["agent_name"], "Applied Despite Error")

                host.session.apply_config = original_apply
                repaired = host.save_settings(changed, expected_revision=2)
                self.assertEqual(repaired["settings_revision"], 3)
                self.assertEqual(repaired["settings_issue"], "")
                self.assertTrue(repaired["ready"])

                invalid = release_settings(home / "workspace")
                invalid["unknown_field"] = "no"
                with self.assertRaises(ConfigError):
                    host.save_settings(invalid, expected_revision=3)
                self.assertEqual(host.snapshot()["settings_revision"], 3)
                self.assertEqual(host.config.agent_name, "Applied Despite Error")
            finally:
                host.shutdown()


class AutonomyLifecycleTests(unittest.TestCase):
    def test_autonomy_start_failure_is_reported_and_retry_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            original_start = host.autonomy.start

            def broken_start():
                raise RuntimeError("floor worker could not start")

            host.autonomy.start = broken_start
            try:
                first = host.save_settings(release_settings(home / "workspace"))
                enabled = release_settings(home / "workspace")
                enabled["advanced"] = {"autonomy": {"enabled": True, "tick_seconds": 3600}}
                failed = host.save_settings(enabled, expected_revision=first["settings_revision"],
                                            autonomy_switch='operator')
                self.assertTrue(failed["settings"]["advanced"]["autonomy"]["enabled"])
                self.assertFalse(failed["ready"])
                self.assertIn("autonomy apply failed", failed["settings_issue"])
                self.assertIsNone(host.autonomy.thread)

                host.autonomy.start = original_start
                repaired = host.save_settings(enabled, expected_revision=failed["settings_revision"],
                                              autonomy_switch='operator')
                self.assertEqual(repaired["settings_issue"], "")
                self.assertIsNotNone(host.autonomy.thread)
            finally:
                host.shutdown()

    def test_advanced_json_and_toolbar_share_one_lifecycle_path(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            host = make_host(home)
            started = []
            closed = []
            original_start = host.autonomy.start
            original_close = host.autonomy.close

            def start():
                started.append(1)
                original_start()

            def close():
                closed.append(1)
                original_close()

            host.autonomy.start = start
            host.autonomy.close = close
            try:
                enabled = release_settings(home / "workspace")
                enabled["advanced"] = {"autonomy": {"enabled": True, "tick_seconds": 3600}}
                snapshot = host.save_settings(enabled, autonomy_switch='operator')
                self.assertTrue(snapshot["autonomy"]["enabled"])
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(host.autonomy.thread)

                disabled = release_settings(home / "workspace")
                disabled["advanced"] = {"autonomy": {"enabled": False, "tick_seconds": 3600}}
                snapshot = host.save_settings(disabled, autonomy_switch='operator')
                self.assertFalse(snapshot["autonomy"]["enabled"])
                self.assertEqual(len(closed), 1)
                self.assertIsNone(host.autonomy.thread)

                enabled_again = release_settings(home / "workspace")
                enabled_again["advanced"] = {"autonomy": {"enabled": True, "tick_seconds": 3600}}
                snapshot = host.save_settings(enabled_again, autonomy_switch='operator')
                self.assertEqual(len(started), 2)
                self.assertIsNotNone(host.autonomy.thread)
                revision = snapshot["settings_revision"]

                toggled = host.set_autonomy(False, expected_revision=revision)
                self.assertFalse(toggled["settings"]["advanced"]["autonomy"]["enabled"])
                self.assertFalse(toggled["autonomy"]["enabled"])

                with self.assertRaises(RuntimeError):
                    host.set_autonomy(True, expected_revision=revision)
                self.assertFalse(host.snapshot()["autonomy"]["enabled"])

                toolbar = host.set_autonomy(True)
                self.assertTrue(toolbar["autonomy"]["enabled"])
                self.assertTrue(toolbar["settings"]["advanced"]["autonomy"]["enabled"])
            finally:
                host.shutdown()


if __name__ == "__main__":
    unittest.main()

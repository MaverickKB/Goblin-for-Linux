"""Host-level tests: live approvals, settings adoption and voice leases."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import default_settings
from goblin_core.host import GoblinHost


class FakeVoiceClient:
    def __init__(self, settings):
        self.settings = settings
        self._secret = "server-secret"
        self.calls = 0

    def mint(self):
        self.calls += 1
        return {"value": "client-secret-token", "expires_at": time.time() + 60}


def make_host(temp):
    home = Path(temp)
    host = GoblinHost(home, ROOT / "product.json")
    host.voice.client = FakeVoiceClient(host.config.voice)
    return host


class HostTests(unittest.TestCase):
    def test_stream_tokens_do_not_invalidate_settings_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                host._emit("activity", {"phase": "model_start"})
                revision = host.snapshot()["revision"]
                host._stream("text", "A response")
                host._stream("status", "Connected")
                self.assertEqual(host.snapshot()["revision"], revision)
                values = host.config.settings_dict()
                values["agent_name"] = "Rook"
                saved = host.save_settings(values)
                self.assertGreater(saved["revision"], revision)
                host._stream("text", " continues")
                self.assertEqual(host.snapshot()["revision"], saved["revision"])
            finally:
                host.shutdown()

    def test_live_approval_and_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            holder = {}
            thread = threading.Thread(target=lambda: holder.setdefault(
                "record", host.execute_tool("write_file", {"path": "approved.txt", "text": "yes"})))
            thread.start()
            approval = None
            for _ in range(50):
                pending = host.pending_approvals()
                if pending:
                    approval = pending[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(approval)
            self.assertEqual(approval["name"], "write_file")
            self.assertTrue(host.snapshot()["pending_approvals"])
            host.resolve_approval(approval["id"], True)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(holder["record"]["success"])
            self.assertEqual((Path(host.config.workspace) / "approved.txt").read_text(), "yes")
            self.assertFalse(host.snapshot()["pending_approvals"])
            host.shutdown()

    def test_settings_save_adopts_disabled_tools_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            values["workspace"] = str(Path(temp) / "workspace")
            values["agent_name"] = "Rook"
            values["advanced"] = {"tools": {"disabled": ["delete_path"]},
                                  "skills": {"enabled": True},
                                  "mcp_servers": {},
                                  "remotes": {}}
            snapshot = host.save_settings(values)
            self.assertEqual(snapshot["settings"]["agent_name"], "Rook")
            self.assertEqual(snapshot["settings"]["advanced"], values["advanced"])
            names = {tool["name"] for tool in snapshot["tools"]}
            self.assertIn("delete_path", names)
            delete_tool = next(tool for tool in snapshot["tools"] if tool["name"] == "delete_path")
            self.assertFalse(delete_tool["enabled"])
            self.assertEqual(delete_tool["origin"], "built-in")
            self.assertIn("write_file", names)
            self.assertEqual(names, set(host.session.tools._tools))
            self.assertNotIn('delete_path', {schema['function']['name'] for schema in host.session.tools.schemas()})
            for tool in snapshot['tools']:
                expected_origin = 'built-in'
                if tool['name'] in host.session._plugin_tools:
                    expected_origin = 'plugin'
                elif tool['name'] in host.session._host_tools:
                    expected_origin = 'host'
                self.assertEqual(tool['origin'], expected_origin)
            host.shutdown()

    def test_unleashed_runs_risky_tools_and_disabled_tools_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            values["workspace"] = str(Path(temp) / "workspace")
            values["approval"] = "unleashed"
            values["advanced"] = {"tools": {"disabled": ["delete_path"]}, "skills": {},
                                  "mcp_servers": {}, "remotes": {}}
            host.save_settings(values)
            record = host.execute_tool("write_file", {"path": "yolo.txt", "text": "yes"})
            self.assertTrue(record["success"])
            self.assertFalse(host.pending_approvals())
            refused = host.execute_tool("delete_path", {"path": "yolo.txt"})
            self.assertFalse(refused["success"])
            self.assertIn("Tool disabled", refused["result"])
            self.assertTrue((Path(host.config.workspace) / "yolo.txt").exists())
            host.shutdown()

    def test_voice_lease_and_tool_events(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            seed = Path(host.config.workspace) / "seed.txt"
            seed.write_text("voice proof")
            lease = host.voice_start()
            self.assertEqual(lease["id"], lease["voice_id"])
            self.assertTrue(lease["websocket_url"].startswith("wss://"))
            self.assertEqual(lease["protocols"][-1], "xai-client-secret.client-secret-token")
            self.assertIn("session.update", lease["session_update"]["type"])
            session = lease["session_update"]["session"]
            self.assertIn("session_task", {tool["name"] for tool in session["tools"]})
            self.assertNotIn("read_file", {tool["name"] for tool in session["tools"]})
            self.assertIn("transcription", json.dumps(session))
            result = host.voice_events(lease["voice_id"], [{
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "u1", "transcript": "read the seed file"}])
            self.assertEqual(result["accepted"], 1)
            self.assertEqual(result["messages"][0]["kind"], "user")
            result = host.voice_events(lease["voice_id"], [{
                "type": "response.function_call_arguments.done",
                "name": "session_task", "call_id": "call-1", "response_id": "resp-1",
                "arguments": json.dumps({"action": "submit", "text": "read the seed file"})}])
            output = result["send"][0]["item"]["output"]
            self.assertIn("turn_id", output)
            self.assertIn("Accepted by the selected session", output)
            stop = host.voice_stop(lease["voice_id"])
            self.assertTrue(stop["stopped"])
            with self.assertRaises(Exception):
                host.voice_events(lease["voice_id"], [])
            host.shutdown()

    def test_autonomy_tick_composes_the_shipped_floor(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            values["workspace"] = str(Path(temp) / "workspace")
            values["floor_interval_seconds"] = 3600
            host.save_settings(values)
            host.set_autonomy(True)
            turn_id = host.autonomy.tick(force_floor=True)
            self.assertTrue(turn_id)
            self.assertGreater(host.autonomy.status()["last_wake"], 0)
            for _ in range(100):
                if not host.busy():
                    break
                time.sleep(0.05)
            host.set_autonomy(False)
            host.shutdown()

    def test_autonomy_fence_blocks_an_immediate_wake(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            values["workspace"] = str(Path(temp) / "workspace")
            values["floor_interval_seconds"] = 5
            host.save_settings(values)
            host.set_autonomy(True)
            host.autonomy.fence()
            self.assertIsNone(host.autonomy.tick(force_floor=True))
            self.assertTrue(host.snapshot()["autonomy"]["enabled"])
            host.set_autonomy(False)
            self.assertFalse(host.snapshot()["autonomy"]["enabled"])
            host.shutdown()


if __name__ == "__main__":
    unittest.main()

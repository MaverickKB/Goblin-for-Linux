"""End-to-end NDJSON pipe smoke: real subprocess, real ChatLog, real tools."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import default_settings


FAKE_CLAUDE = """#!/bin/sh
exec /usr/bin/python3 -c 'print("{\\"type\\":\\"result\\",\\"subtype\\":\\"success\\",\\"is_error\\":false,\\"result\\":\\"Hello from the fake CLI.\\",\\"usage\\":{\\"input_tokens\\":12,\\"output_tokens\\":6}}")'
"""


class ModelHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        ModelHandler.calls += 1
        if ModelHandler.calls == 1:
            body = {"choices": [{"message": {"content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "seed.txt"})}}]},
                "finish_reason": "tool_calls"}], "usage": {}}
        else:
            body = {"choices": [{"message": {"content": "the seed says: " + ModelHandler.seed},
                                 "finish_reason": "stop"}], "usage": {}}
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return


class Pipe:
    def __init__(self, home, script=None):
        self.home = home
        entry = [str(ROOT / "Runtime" / "goblin_bridge.py")] if script is None else ["-c", script]
        self.process = subprocess.Popen(
            [sys.executable, "-u", *entry,
             "--home", str(home), "--manifest", str(ROOT / "product.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(ROOT))
        self.frames = []
        self.condition = threading.Condition()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.condition:
                self.frames.append(frame)
                self.condition.notify_all()

    def send(self, request_id, method, params=None):
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def wait_id(self, request_id, timeout=15):
        deadline = time.time() + timeout
        with self.condition:
            while time.time() < deadline:
                for frame in self.frames:
                    if frame.get("id") == request_id:
                        return frame
                self.condition.wait(0.1)
        raise AssertionError("no response for " + request_id + "; frames=" + json.dumps(self.frames[-8:]))

    def wait_event(self, event, predicate=None, timeout=15):
        deadline = time.time() + timeout
        with self.condition:
            while time.time() < deadline:
                for frame in self.frames:
                    if frame.get("event") == event and (predicate is None or predicate(frame.get("data") or {})):
                        return frame
                self.condition.wait(0.1)
        raise AssertionError("no event " + event)

    def wait_settled(self, timeout=20):
        deadline = time.time() + timeout
        with self.condition:
            while time.time() < deadline:
                snapshots = [f["data"] for f in self.frames if f.get("event") == "snapshot"]
                if snapshots and not snapshots[-1]["busy"]:
                    return snapshots[-1]
                self.condition.wait(0.1)
        raise AssertionError("turn did not settle")

    def history(self):
        request_id = "history-" + str(time.monotonic_ns())
        self.send(request_id, "history/page")
        return self.wait_id(request_id)["result"]["messages"]

    def gist(self):
        import base64
        data = b''
        params = {}
        while True:
            request_id = 'gist-' + str(time.monotonic_ns())
            self.send(request_id, 'history/gist', params)
            chunk = self.wait_id(request_id)['result']
            data += base64.b64decode(chunk['data'])
            if chunk['next_offset'] == chunk['total']:
                return data.decode('utf-8')
            params = {'token': chunk['token'], 'offset': chunk['next_offset']}

    def stop(self):
        try:
            self.send("shutdown-request", "shutdown")
            self.process.wait(timeout=10)
        except Exception:
            self.process.kill()
        error = self.process.stderr.read() if self.process.stderr is not None else ""
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if self.process.returncode not in (0, None):
            raise AssertionError("bridge exited " + str(self.process.returncode) + ": " + error[-2000:])


class BridgeSmokeTests(unittest.TestCase):
    def test_large_archive_history_read_does_not_block_snapshot_or_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            log = home / 'chat' / 'goblin' / 'log'
            log.mkdir(parents=True)
            with (log / '2026-01-01.jsonl').open('w') as output:
                for index in range(3000):
                    text = 'Saved message ' + str(index)
                    output.write(json.dumps({'i': index, 'kind': 'user', 'text': text,
                                             'size': len(text.encode()), 'date': '2026-01-01T00:00:00+00:00',
                                             'meta': {}}) + '\n')
            script = """
import sys
import time
from pathlib import Path
sys.path.insert(0, %r)
from goblin_core.host import GoblinHost
import goblin_bridge
original = GoblinHost.history_page
def blocked(self, *args, **kwargs):
    (self.home / 'history-entered').touch()
    while not (self.home / 'history-release').exists():
        time.sleep(.01)
    return original(self, *args, **kwargs)
GoblinHost.history_page = blocked
goblin_bridge.main()
""" % str(ROOT / 'Runtime')
            pipe = Pipe(home, script=script)
            try:
                pipe.send('history-blocked', 'history/page')
                deadline = time.monotonic() + 15
                while not (home / 'history-entered').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((home / 'history-entered').exists())
                pipe.send('state-small', 'snapshot')
                state = pipe.wait_id('state-small', timeout=5)['result']
                self.assertEqual(state['history_count'], 3000)
                self.assertNotIn('messages', state)
                self.assertNotIn('gist', state)
                self.assertLess(len(json.dumps(state).encode()), 100000)
                pipe.send('stop-responsive', 'chat/stop')
                self.assertIn('result', pipe.wait_id('stop-responsive', timeout=5))
                self.assertFalse(any(frame.get('id') == 'history-blocked' for frame in pipe.frames))
                (home / 'history-release').touch()
                page = pipe.wait_id('history-blocked')['result']
                self.assertTrue(page['has_more'])
                self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 65536)
            finally:
                (home / 'history-release').touch()
                pipe.stop()

    def test_coalesced_control_frames_are_not_stranded_in_stdin(self):
        with tempfile.TemporaryDirectory() as temp:
            pipe = Pipe(Path(temp))
            try:
                frames = [{"id": "first", "method": "bootstrap"},
                          {"id": "second", "method": "snapshot"}]
                for frame in frames:
                    pipe.process.stdin.write(json.dumps(frame) + "\n")
                pipe.process.stdin.flush()
                self.assertIn("result", pipe.wait_id("first"))
                self.assertIn("result", pipe.wait_id("second"))
            finally:
                pipe.stop()

    def test_http_chat_and_tool_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            workspace = home / "workspace"
            workspace.mkdir()
            (workspace / "seed.txt").write_text("green")
            ModelHandler.seed = "green"
            ModelHandler.calls = 0
            server = HTTPServer(("127.0.0.1", 0), ModelHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            settings = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            settings.update({
                "agent_name": "HTTP Goblin",
                "workspace": str(workspace),
                "model_base_url": "http://127.0.0.1:" + str(server.server_port),
                "model": "fake-model",
                "api_kind": "chat_completions",
                "advanced": {},
            })
            pipe = Pipe(home)
            try:
                pipe.send("save", "settings/save", {"settings": settings})
                pipe.wait_id("save")
                pipe.send("chat", "chat/send", {"text": "read the seed"})
                pipe.wait_id("chat")
                settled = pipe.wait_settled(20)
                self.assertIn("the seed says: green", [message["text"] for message in pipe.history()])
                self.assertGreaterEqual(ModelHandler.calls, 2)
                self.assertFalse(settled["busy"])
            finally:
                pipe.stop()
                server.shutdown()
                server.server_close()

    def test_full_pipe(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            bin_dir = Path(temp) / "bin"
            bin_dir.mkdir()
            claude = bin_dir / "claude"
            claude.write_text(FAKE_CLAUDE)
            claude.chmod(0o755)
            settings = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
            settings.update({
                "agent_name": "Smoke Goblin",
                "user_instructions": "Answer plainly.",
                "workspace": str(home / "workspace"),
                "api_kind": "cli_worker",
                "cli_command": str(claude),
                "advanced": {"tools": {"disabled": ["delete_path"]}, "skills": {"enabled": True},
                             "mcp_servers": {}, "remotes": {}},
            })
            pipe = Pipe(home)
            try:
                pipe.send("bootstrap", "bootstrap")
                boot = pipe.wait_id("bootstrap")["result"]
                self.assertEqual(boot["model"], "")
                self.assertIn("settings", boot)
                self.assertIn("history_count", boot)

                pipe.send("save", "settings/save", {"settings": settings})
                saved = pipe.wait_id("save")["result"]
                self.assertEqual(saved["settings"]["advanced"], settings["advanced"])
                disabled_tool = next(tool for tool in saved["tools"] if tool["name"] == "delete_path")
                self.assertFalse(disabled_tool["enabled"])
                self.assertEqual(saved["settings"]["agent_name"], "Smoke Goblin")

                pipe.send("chat", "chat/send", {"text": "Say hello."})
                turn_id = pipe.wait_id("chat")["result"]["turn_id"]
                settled = pipe.wait_settled()
                texts = [message["text"] for message in pipe.history()]
                self.assertIn("Hello from the fake CLI.", texts)
                self.assertIn("Say hello.", pipe.gist())
                self.assertFalse(settled["busy"])
                self.assertEqual(pipe.wait_id("chat")["result"]["turn_id"], turn_id)

                note = home / "note.txt"
                note.write_text("attached proof")
                pipe.send("chat2", "chat/send", {"text": "see attached", "attachments": [str(note)]})
                pipe.wait_id("chat2")
                pipe.wait_settled(20)
                refs = list((home / "chat" / "goblin" / "attachments").glob("*.ref"))
                self.assertEqual(len(refs), 1)
            finally:
                pipe.stop()

            # Relaunch: the same chat and gist come back from ChatLog.
            again = Pipe(home)
            try:
                again.send("boot2", "bootstrap")
                boot2 = again.wait_id("boot2")["result"]
                self.assertIn("Hello from the fake CLI.", [message["text"] for message in again.history()])
                self.assertIn("Say hello.", again.gist())
                self.assertGreaterEqual(len(again.history()), 2)

                again.send("tool1", "tool/execute",
                           {"name": "write_file", "arguments": {"path": "approved.txt", "text": "proof"}})
                approval = again.wait_event("approval")
                self.assertEqual(approval["data"]["name"], "write_file")
                again.send("snap1", "snapshot")
                snap = again.wait_id("snap1")["result"]
                self.assertEqual(len(snap["pending_approvals"]), 1)
                again.send("resolve", "approval/resolve",
                           {"id": approval["data"]["id"], "accepted": True})
                self.assertTrue(again.wait_id("resolve")["result"]["accepted"])
                result = again.wait_id("tool1")["result"]
                self.assertTrue(result["success"])
                self.assertEqual((home / "workspace" / "approved.txt").read_text(), "proof")

                again.send("floor-bad", "floor/save", {"definition": "{bad json"})
                self.assertIn("error", again.wait_id("floor-bad"))
                again.send("floor-ok", "floor/save", {"definition": json.dumps({
                    "compose": {"world_view": "", "world_view_max_chars": 2000, "world_view_intro": "",
                                "wake_score": 1.0, "stale_seconds": 3600, "stale_label": "an hour",
                                "footer": "Be here.", "pressure_intro": "Pressure:",
                                "coordination_paragraph": "", "quiet_paragraph": "Quiet."},
                    "sources": {"timer": True}, "rules": []})})
                floor = again.wait_id("floor-ok")["result"]
                self.assertEqual(floor["status"]["floor_source"], "file")

                again.send("auto-on", "autonomy/set", {"enabled": True})
                self.assertTrue(again.wait_id("auto-on")["result"]["autonomy"]["enabled"])
                again.send("auto-off", "autonomy/set", {"enabled": False})
                self.assertFalse(again.wait_id("auto-off")["result"]["autonomy"]["enabled"])

                again.send("acct", "account/status", {"provider": "xai"})
                account = again.wait_id("acct")["result"]
                self.assertIn(account["grant"]["state"],
                              ("missing", "expired", "current", "unavailable"))
                self.assertNotIn("access_token", json.dumps(account))

                again.send("unknown", "not/a/method")
                self.assertIn("error", again.wait_id("unknown"))
            finally:
                again.stop()


if __name__ == "__main__":
    unittest.main()

"""Advanced settings are real: MCP, skill shelf, remotes and attachments."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import ConfigError, default_settings
from goblin_core.host import GoblinHost


FAKE_MCP = """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'fake'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Echo text',
                             'inputSchema': {'type': 'object',
                                             'properties': {'text': {'type': 'string'}},
                                             'required': ['text']}}]}
    elif method == 'tools/call':
        text = request['params'].get('arguments', {}).get('text', '')
        result = {'content': [{'type': 'text', 'text': 'echo:' + text}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}) + '\\n')
    sys.stdout.flush()
"""


class IntegrationTests(unittest.TestCase):
    def _settings(self, home):
        values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
        values["workspace"] = str(home / "workspace")
        return values

    def test_mcp_server_tools_are_registered_and_called(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            server = home / "fake_mcp.py"
            server.write_text(FAKE_MCP)
            values = self._settings(home)
            values["advanced"] = {
                "tools": {},
                "skills": {},
                "mcp_servers": {"fake": {"command": [sys.executable, str(server)],
                                         "session_tools": True, "safe_tools": ["echo"]}},
                "remotes": {},
            }
            host = GoblinHost(home, ROOT / "product.json")
            try:
                snapshot = host.save_settings(values)
                names = {tool["name"] for tool in snapshot["tools"]}
                self.assertIn("fake__echo", names)
                record = host.execute_tool("fake__echo", {"text": "hi"})
                self.assertTrue(record["success"])
                self.assertEqual(record["result"], "echo:hi")
            finally:
                host.shutdown()

    def test_skill_shelf_and_attachment_store(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            skill = home / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: A demo skill.\n---\nDo the demo.\n")
            values = self._settings(home)
            values["advanced"] = {"tools": {}, "skills": {"enabled": True, "shelf": str(home / "skills")},
                                  "mcp_servers": {}, "remotes": {}}
            host = GoblinHost(home, ROOT / "product.json")
            try:
                snapshot = host.save_settings(values)
                names = {tool["name"] for tool in snapshot["tools"]}
                self.assertIn("skill_load", names)
                loaded = host.session.execute_tool("skill_load", {"name": "demo"})
                self.assertIn("Do the demo.", loaded["result"])
            finally:
                host.shutdown()

    def test_attachment_path_is_ingested_into_the_session_store(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            source = home / "note.txt"
            source.write_text("attachment proof")
            host = GoblinHost(home, ROOT / "product.json")
            try:
                refs = host._ingest_attachments([str(source)])
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0]["name"], "note.txt")
                stored = list((host.session.root / "attachments").glob("*.ref"))
                self.assertEqual(len(stored), 1)
                notes, images = host.session._attachment_content(refs)
                self.assertIn("attachment proof", notes[0])
                self.assertEqual(images, [])
            finally:
                host.shutdown()

    def test_remote_tools_register_and_bad_secret_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = self._settings(home)
            values["advanced"] = {
                "tools": {}, "skills": {}, "mcp_servers": {},
                "remotes": {"box": {"host": "example.invalid", "user": "ops"}},
            }
            host = GoblinHost(home, ROOT / "product.json")
            try:
                snapshot = host.save_settings(values)
                names = {tool["name"] for tool in snapshot["tools"]}
                self.assertIn("remote_shell", names)
                self.assertIn("remote_read_file", names)
                bad = self._settings(home)
                bad["advanced"] = {"tools": {}, "skills": {}, "mcp_servers": {},
                                   "remotes": {"box": {"host": "example.invalid", "password": "no"}}}
                with self.assertRaises(ConfigError):
                    host.save_settings(bad)
            finally:
                host.shutdown()


if __name__ == "__main__":
    unittest.main()

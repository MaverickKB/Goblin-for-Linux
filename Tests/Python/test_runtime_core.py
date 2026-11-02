"""Focused tests for the independent Goblin runtime core."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.aethermind import AetherMindClient
from goblin_core.aethermind_direct import DirectMcpClient
from goblin_core.capture import Capture
from goblin_core.config import ConfigError, default_settings, load_config, normalize_settings, write_settings
from goblin_core.capture_classification import CaptureClassifier
from goblin_core.log import ChatLog
from goblin_core.tools import child_environment
from goblin_core.tree import SummaryTree
from goblin_core.view import View
from goblin_core.vendor_aethermind.aethermind.tree import build_tree


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


class BoundaryTests(unittest.TestCase):
    def test_runtime_has_no_server_imports(self):
        forbidden = ("http.server", "socketserver", "fastapi", "flask", "uvicorn", "bundled_engine")
        for path in (ROOT / "Runtime").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn("import " + name, text, str(path))


class ChatGistTests(unittest.TestCase):
    def test_chatlog_view_and_summary_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            tree = SummaryTree(root, log, node_bytes=128)
            view = View(root, tree, high_bytes=10000, low_bytes=5000)
            log.subscribe(view.on_message)
            log.append("user", "one")
            log.append("agent", "two")
            log.append("tool", '{"name":"read_file"}')
            self.assertEqual(log.count(), 3)
            self.assertEqual(log.get(0).kind, "user")
            self.assertIn("0+1|[source:unattributed_input] one", view.render())
            self.assertEqual(tree.node(0, 0), "one")
            self.assertIn("0+1|[source:unattributed_input] one", view.render())
            self.assertEqual(view.zoom(1, 1), "two")
            with self.assertRaises(ValueError):
                tree.set_node(0, 0, "changed")
            view.save()
            log.close()

    def test_log_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            log.append("user", "persisted")
            log.close()
            again = ChatLog(root / "log")
            self.assertEqual(again.count(), 1)
            self.assertEqual(again.get(0).text, "persisted")
            again.close()


class ConfigTests(unittest.TestCase):
    def test_blank_model_bootstraps_and_advanced_roundtrips(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            self.assertEqual(values["model"], "")
            self.assertEqual(values["api_kind"], "chat_completions")
            values["advanced"] = {
                "tools": {"disabled": ["delete_path"], "clip_chars": 4000},
                "skills": {"enabled": False, "extra_dirs": [str(home / "skills")]},
                "mcp_servers": {"docs": {"command": ["/bin/echo"], "session_tools": True}},
                "remotes": {"box": {"host": "example.invalid", "user": "ops"}},
            }
            saved = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(saved.advanced, values["advanced"])
            loaded = load_config(home, ROOT / "product.json")
            self.assertEqual(loaded.settings_dict()["advanced"], values["advanced"])
            self.assertEqual(Path(loaded.aethermind.project).resolve(), (home / "aethermind").resolve())
            self.assertEqual((home / "config.json").exists(), True)

    def test_unknown_and_secret_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["bogus"] = 1
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)
            values = default_settings(manifest())
            values["model_api_key"] = "sk-no"
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)
            values = default_settings(manifest())
            values["advanced"] = {"tools": {"bogus": 1}}
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)
            values = default_settings(manifest())
            values["advanced"] = {"unknown_section": {}}
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)
            values = default_settings(manifest())
            values["approval"] = "sometimes"
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)

    def test_legacy_approval_names_load_as_ask_and_unleashed(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for old, new in (("safe", "ask"), ("yolo", "unleashed")):
                values = default_settings(manifest())
                values["approval"] = old
                saved = normalize_settings(values, manifest(), home)
                self.assertEqual(saved["approval"], new)
                loaded = write_settings(home, ROOT / "product.json", {"approval": old})
                self.assertEqual(loaded.approval, new)
                self.assertEqual(loaded.tools.approval, new)
                self.assertEqual(loaded.settings_dict()["approval"], new)
                again = load_config(home, ROOT / "product.json")
                self.assertEqual(again.approval, new)


class DirectAetherMindTests(unittest.TestCase):
    def test_write_read_brief_inheritance_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            project = str(Path(temp) / "memory")
            client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
            capabilities = client.call("capabilities", {})
            self.assertEqual(capabilities["runtime"]["runtime_id"], "aethermind-python")
            self.assertEqual(capabilities["runtime"]["contract_version"], "adapter-v1")
            self.assertTrue(capabilities["runtime"]["layer_types"])
            detected = client.detect_tools()
            self.assertEqual(detected["inheritance"], "prepare_inheritance")
            self.assertEqual(detected["write"], "write_layer")
            self.assertEqual(detected["zoom"], "zoom")
            written = client.write_layer(project, "req-1", {
                "type": "decision", "body": "Use the direct library adapter.",
                "ctx": "goblin/test", "author": "tester", "conf": 1.0, "markers": ["!"]})
            client.write_layer(project, "req-2", {
                "type": "discovery", "body": "Second record for the tree.",
                "ctx": "goblin/test", "author": "tester", "conf": 1.0, "markers": []})
            self.assertTrue(written["success"])
            read = client.read_layers(project, query="direct", last_n=5)
            self.assertEqual(read["matched"], 1)
            self.assertIn("direct library", read["layers"][0]["body"])
            brief = client.brief(project, budget=1000)
            self.assertTrue(brief["success"])
            inherited = client.prepare_inheritance(project, None, 2000)
            self.assertTrue(inherited["success"])
            result = build_tree(project, compactor=lambda content, _anchor, _aim, limit: "summary")
            self.assertGreaterEqual(result.get("nodes_built", 0), 1)
            view = client.tree_view(project, 1000)
            self.assertTrue(view["success"])
            self.assertIn("view", view)
            zoom = client.tree_zoom(project, 0, 0)
            self.assertTrue(zoom["success"])

    def test_capture_writes_a_layer_and_clear_debt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = str(root / "memory")
            client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
            capture = Capture(client, root / "outbox", project, author="goblin")
            capture.turn_start({"message": "do the thing", "turn": 1})
            capture.post_tool({"name": "read_file", "id": "c1", "result": "ok", "success": True})
            capture.turn_end({"reply": "done", "success": True})
            self.assertEqual(capture.debt(), [])
            read = client.read_layers(project, last_n=5)
            self.assertEqual(read["count"], 1)
            layer = read["layers"][0]
            self.assertEqual(layer["type"], "discovery")
            self.assertTrue(layer["body"].startswith("Source attribution:"))
            self.assertIn("Type: discovery", layer["body"])
            self.assertIn("goblin-origin:agent_inference", layer["markers"])
            self.assertIn("Outcome: done", layer["body"])
            self.assertIn("goblin/turn/session/", layer["ctx"])

    def test_capture_skips_a_session_with_no_completed_turns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = str(root / "memory")
            client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
            client.write_layer(project, "seed", {
                "type": "discovery", "body": "seed record", "ctx": "goblin/test",
                "author": "tester", "conf": 1.0, "markers": []})
            capture = Capture(client, root / "outbox", project, author="goblin")
            capture.session_end({})
            empty = client.read_layers(project, last_n=5)["layers"]
            self.assertEqual(len(empty), 1)
            for layer in empty:
                self.assertFalse(layer["body"].startswith("Session "))

            capture.turn_start({"message": "do the thing", "turn": 1})
            capture.turn_end({"reply": "done", "success": True})
            capture.session_end({})
            worked = client.read_layers(project, last_n=5)["layers"]
            session_bodies = [layer["body"] for layer in worked
                              if layer["body"].startswith("Session ")]
            self.assertEqual(len(session_bodies), 1)


class ChildEnvironmentTests(unittest.TestCase):
    def test_app_credential_paths_are_removed(self):
        base = {"PATH": "/bin", "GOBLIN_KEYCHAIN_HELPER": "/app/helper",
                "GOBLIN_RESOURCES": "/app"}
        merged = child_environment({"EXTRA": "1"}, base=base)
        self.assertNotIn("GOBLIN_KEYCHAIN_HELPER", merged)
        self.assertNotIn("GOBLIN_RESOURCES", merged)
        self.assertEqual(merged["EXTRA"], "1")
        self.assertEqual(merged["PATH"], "/bin")

    def test_explicit_helper_path_is_removed(self):
        base = {"PATH": "/bin"}
        merged = child_environment({"GOBLIN_KEYCHAIN_HELPER": "/model/supplied"}, base=base)
        self.assertNotIn("GOBLIN_KEYCHAIN_HELPER", merged)


class JudgeTests(unittest.TestCase):
    def test_repeated_outcome_is_a_duplicate(self):
        judge = CaptureClassifier()
        repeated = judge.memory("q", "Same reply text.", [], {},
                                existing_layers=["Same reply text."])
        self.assertEqual(repeated["duplicate_of_existing_layer"], 1.0)
        different = judge.memory("q", "A different outcome.", [], {},
                                 existing_layers=["Same reply text."])
        self.assertEqual(different["duplicate_of_existing_layer"], 0.0)


if __name__ == "__main__":
    unittest.main()

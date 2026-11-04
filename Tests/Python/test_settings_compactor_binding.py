"""The settings document the Linux UI writes must bind compaction correctly.

The payloads come from src/settings-draft.js itself, so a UI change that drifts
from the engine contract fails here instead of shipping a document the engine
refuses.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import ConfigError, load_config, normalize_settings, write_settings


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


def ui_payloads():
    node = shutil.which("node")
    if node is None:
        raise unittest.SkipTest("node is required to compose the UI settings payload")
    result = subprocess.run(
        [node, str(ROOT / "Tests" / "js" / "emit-settings-draft.mjs")],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError("settings-draft.js failed: " + result.stderr[-2000:])
    return json.loads(result.stdout)


class SettingsCompactorBindingTests(unittest.TestCase):
    def setUp(self):
        self.payloads = ui_payloads()

    def save(self, name, home):
        return write_settings(home, ROOT / "product.json", self.payloads[name])

    def test_compactor_route_from_the_ui_binds_its_own_model(self):
        with tempfile.TemporaryDirectory() as temp:
            saved = self.save("compactor_provider", Path(temp))
            connection, endpoint, model, _kind, _cli, _ws, reasoning, budget = (
                saved.model_binding(compaction=True))
            self.assertEqual(connection, "xai")
            self.assertEqual(model, "grok-4.20-0309-non-reasoning")
            self.assertEqual(endpoint, "https://api.x.ai/v1")
            self.assertEqual(reasoning, "off")
            self.assertEqual(budget, 0)
            self.assertEqual(saved.chat_model(), "grok-4.7")

    def test_compactor_same_as_chat_inherits_the_chat_route(self):
        with tempfile.TemporaryDirectory() as temp:
            saved = self.save("compactor_same_as_chat", Path(temp))
            self.assertEqual(saved.compactor.connection, "")
            self.assertEqual(saved.compactor.account_models, {})
            chat = saved.model_binding()
            compaction = saved.model_binding(compaction=True)
            self.assertEqual(chat[2], "chat-model")
            self.assertEqual(compaction[0], chat[0])
            self.assertEqual(compaction[2], chat[2])

    def test_compactor_custom_endpoint_model_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.save("compactor_custom_endpoint", home)
            loaded = load_config(home, ROOT / "product.json")
            self.assertEqual(loaded.compactor.model, "fast-compress")
            self.assertEqual(loaded.model_binding(compaction=True)[2], "fast-compress")

    def test_chat_provider_selection_writes_the_account_model_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            saved = self.save("chat_provider", Path(temp))
            self.assertEqual(saved.model_connection, "xai")
            self.assertEqual(saved.chat_model(), "grok-4.7")
            self.assertEqual(saved.model_binding()[1], "https://api.x.ai/v1")

    def test_switching_the_compression_route_drops_the_old_route_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            saved = self.save("compactor_route_changed_from_custom", Path(temp))
            compactor = saved.settings_dict()["advanced"]["compactor"]
            self.assertEqual(compactor["connection"], "xai")
            self.assertNotIn("model", compactor)
            self.assertNotIn("model_base_url", compactor)
            self.assertEqual(compactor["workers"], 3)
            self.assertEqual(saved.model_binding(compaction=True)[2],
                             "grok-4.20-0309-non-reasoning")

    def test_a_compactor_provider_without_a_model_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ConfigError):
                normalize_settings(self.payloads["compactor_provider_without_model"],
                                   manifest(), Path(temp))


if __name__ == "__main__":
    unittest.main()

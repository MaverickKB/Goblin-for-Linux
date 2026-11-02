"""Core selection keeps local and server-owned paths independent."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.config import Config, ConfigError, default_settings, normalize_settings, write_settings, load_config


class MemoryConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "product.json").read_text())

    def test_default_remains_embedded_and_does_not_create_a_remote_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            config = Config(home, self.manifest, default_settings(self.manifest))
            self.assertEqual(config.aethermind.mode, "embedded")
            self.assertEqual(config.aethermind.project, str(home / "aethermind"))
            self.assertEqual(config.aethermind.endpoint, "")
            self.assertTrue(config.aethermind.create_project)

    def test_saved_remote_and_local_targets_remain_separate_when_switching(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            values = default_settings(self.manifest)
            values["advanced"]["aethermind"] = {
                "mode": "remote", "endpoint": "http://memory.example.test/mcp",
                "project": "remote-project-key", "local_project": "imported-core",
                "token_env": "MEMORY_ACCESS",
            }
            saved = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(saved.aethermind.project, "remote-project-key")
            self.assertEqual(saved.aethermind.local_project, str(home / "imported-core"))
            self.assertFalse(saved.aethermind.create_project)
            loaded = load_config(home, ROOT / "product.json")
            self.assertEqual(loaded.aethermind.project, saved.aethermind.project)
            values["advanced"]["aethermind"]["mode"] = "embedded"
            local = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(local.aethermind.project, str(home / "imported-core"))
            self.assertNotEqual(local.aethermind.project, "remote-project-key")
            self.assertFalse(local.aethermind.create_project)

    def test_invalid_remote_addresses_and_plaintext_credentials_are_not_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = [
                {"mode": "remote", "project": "project"},
                {"mode": "remote", "endpoint": "http://memory.example.test/mcp"},
                {"mode": "remote", "endpoint": "http://user:password@memory.example.test/mcp", "project": "project"},
                {"mode": "embedded", "api_key": "not-allowed"},
            ]
            for connection in cases:
                with self.subTest(connection=connection):
                    values = default_settings(self.manifest)
                    values["advanced"]["aethermind"] = connection
                    with self.assertRaises(ConfigError):
                        normalize_settings(values, self.manifest, Path(temp))

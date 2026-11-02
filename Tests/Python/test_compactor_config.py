"""Focused tests for optional advanced.compactor model binding."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import (
    ConfigError,
    default_settings,
    load_config,
    normalize_settings,
    write_settings,
)


def manifest():
    return json.loads((ROOT / "product.json").read_text(encoding="utf-8"))


class CompactorConfigTests(unittest.TestCase):
    def test_omitted_compactor_keeps_defaults_and_chat_inheritance(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["model"] = "chat-model"
            values["model_base_url"] = "http://127.0.0.1:9/v1"
            values["user_instructions"] = "Stay concise."
            values["world_view"] = "Operator desk"
            saved = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(saved.compactor.model, "")
            self.assertEqual(saved.compactor.model_base_url, "")
            self.assertEqual(saved.compactor.api_kind, "chat_completions")
            self.assertEqual(saved.compactor.workers, 2)
            self.assertEqual(saved.compactor.retries, 2)
            self.assertEqual(saved.compactor.context_bytes, 16000)
            self.assertEqual(saved.model, "chat-model")
            self.assertEqual(saved.model_base_url, "http://127.0.0.1:9/v1")

    def test_override_round_trips_and_preserves_main_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["model"] = "chat-model"
            values["model_base_url"] = "http://127.0.0.1:9/v1"
            values["api_kind"] = "responses"
            values["user_instructions"] = "Preserve these instructions."
            values["world_view"] = "Workshop floor"
            values["advanced"] = {
                "tools": {"disabled": ["delete_path"], "clip_chars": 4000},
                "skills": {"enabled": False, "extra_dirs": [str(home / "skills")]},
                "mcp_servers": {
                    "docs": {"command": ["/bin/echo"], "session_tools": True},
                },
                "remotes": {"box": {"host": "example.invalid", "user": "ops"}},
                "compactor": {
                    "workers": 3,
                    "retries": 1,
                    "context_bytes": 20000,
                    "model": "fast-compress",
                    "model_base_url": "http://127.0.0.1:11/v1/",
                    "api_kind": "chat_completions",
                },
            }
            saved = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(saved.model, "chat-model")
            self.assertEqual(saved.model_base_url, "http://127.0.0.1:9/v1")
            self.assertEqual(saved.api_kind, "responses")
            self.assertEqual(saved.user_instructions, "Preserve these instructions.")
            self.assertEqual(saved.world_view, "Workshop floor")
            self.assertEqual(saved.advanced["tools"]["disabled"], ["delete_path"])
            self.assertEqual(saved.advanced["skills"]["enabled"], False)
            self.assertEqual(saved.advanced["mcp_servers"]["docs"]["command"], ["/bin/echo"])
            self.assertEqual(saved.advanced["remotes"]["box"]["host"], "example.invalid")
            self.assertEqual(saved.compactor.model, "fast-compress")
            # Trailing slash is stripped like Backend does for endpoints.
            self.assertEqual(saved.compactor.model_base_url, "http://127.0.0.1:11/v1")
            self.assertEqual(saved.compactor.api_kind, "chat_completions")
            self.assertEqual(saved.compactor.workers, 3)
            self.assertEqual(saved.compactor.retries, 1)
            self.assertEqual(saved.compactor.context_bytes, 20000)

            loaded = load_config(home, ROOT / "product.json")
            self.assertEqual(loaded.settings_dict()["model"], "chat-model")
            self.assertEqual(loaded.settings_dict()["model_base_url"], "http://127.0.0.1:9/v1")
            self.assertEqual(loaded.settings_dict()["user_instructions"], "Preserve these instructions.")
            self.assertEqual(loaded.settings_dict()["world_view"], "Workshop floor")
            self.assertEqual(loaded.settings_dict()["advanced"]["tools"], values["advanced"]["tools"])
            self.assertEqual(loaded.settings_dict()["advanced"]["skills"], values["advanced"]["skills"])
            self.assertEqual(
                loaded.settings_dict()["advanced"]["mcp_servers"],
                values["advanced"]["mcp_servers"],
            )
            self.assertEqual(loaded.settings_dict()["advanced"]["remotes"], values["advanced"]["remotes"])
            self.assertEqual(loaded.compactor.model, "fast-compress")
            self.assertEqual(loaded.compactor.model_base_url, "http://127.0.0.1:11/v1")
            self.assertEqual(loaded.compactor.api_kind, "chat_completions")
            self.assertEqual(loaded.compactor.workers, 3)

    def test_model_only_override_inherits_empty_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["model"] = "chat-model"
            values["model_base_url"] = "http://127.0.0.1:9/v1"
            values["advanced"] = {
                "compactor": {
                    "model": "fast-compress",
                    "api_kind": "responses",
                },
            }
            saved = write_settings(home, ROOT / "product.json", values)
            self.assertEqual(saved.compactor.model, "fast-compress")
            self.assertEqual(saved.compactor.model_base_url, "")
            self.assertEqual(saved.compactor.api_kind, "responses")
            self.assertEqual(saved.model, "chat-model")
            self.assertEqual(saved.model_base_url, "http://127.0.0.1:9/v1")

    def test_endpoint_without_model_is_invalid_partial_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["advanced"] = {
                "compactor": {
                    "model_base_url": "http://127.0.0.1:11/v1",
                },
            }
            with self.assertRaises(ConfigError) as caught:
                normalize_settings(values, manifest(), home)
            self.assertIn("model_base_url requires", str(caught.exception))

    def test_credential_url_and_cli_worker_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["advanced"] = {
                "compactor": {
                    "model": "fast-compress",
                    "model_base_url": "http://user:pass@127.0.0.1:11/v1",
                },
            }
            with self.assertRaises(ConfigError) as caught:
                normalize_settings(values, manifest(), home)
            self.assertIn("credential-free", str(caught.exception))

            values = default_settings(manifest())
            values["advanced"] = {
                "compactor": {
                    "model": "fast-compress",
                    "api_kind": "cli_worker",
                },
            }
            with self.assertRaises(ConfigError) as caught:
                normalize_settings(values, manifest(), home)
            self.assertIn("chat_completions or responses", str(caught.exception))

    def test_unknown_and_secret_compactor_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            values = default_settings(manifest())
            values["advanced"] = {"compactor": {"bogus": 1}}
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)

            values = default_settings(manifest())
            values["advanced"] = {"compactor": {"api_key": "sk-no"}}
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)

            values = default_settings(manifest())
            values["advanced"] = {"compactor": {"workers": 0}}
            with self.assertRaises(ConfigError):
                normalize_settings(values, manifest(), home)


if __name__ == "__main__":
    unittest.main()

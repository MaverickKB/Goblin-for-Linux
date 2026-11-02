"""Secret transport and fresh credential adoption. No real credential access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.keychain import GrantStore, Keychain, KeychainError, KeychainAuthorizationRequired, SecretProvider


class KeychainTests(unittest.TestCase):
    def test_native_write_uses_stdin_and_never_process_arguments(self):
        calls = []

        def run(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"state": "saved"}), "")

        with tempfile.NamedTemporaryFile() as executable:
            with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": executable.name}):
                Keychain(runner=run).put("app.probe", "synthetic-probe")
        arguments, options = calls[0]
        self.assertEqual(len(arguments), 1)
        self.assertNotIn("synthetic-probe", " ".join(arguments))
        request = json.loads(options["input"])
        self.assertEqual(request["value"], "synthetic-probe")
        self.assertEqual(request["service"], "app.probe")

    def test_native_access_error_is_not_reported_as_missing(self):
        def run(arguments, **options):
            return subprocess.CompletedProcess(arguments, 0, json.dumps({
                "state": "error", "message": "Keychain read failed (-25308)."
            }), "")

        with tempfile.NamedTemporaryFile() as executable:
            with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": executable.name}):
                with self.assertRaisesRegex(KeychainError, "-25308"):
                    Keychain(runner=run).get("app.probe")

    def test_model_key_is_optional_and_new_requests_read_current_value(self):
        class Store:
            value = ""
            receipts = {}

            def get(self, service, account):
                value = self.value if account == "https://model.example/v1" else self.receipts.get(account, "")
                return value, "available" if value else "missing"

            def exists(self, service, account):
                value, state = self.get(service, account)
                return "found" if state == "available" else "missing"

            def put(self, service, value, account):
                self.receipts[account] = value

        store = Store()
        provider = SecretProvider({"model": "app.model", "voice": "app.voice"}, keychain=store,
                                  model_endpoint="https://model.example/v1")
        self.assertEqual(provider.model_key(), "")
        store.value = "first-probe"
        self.assertEqual(provider.model_key(), "first-probe")
        store.value = "replacement-probe"
        self.assertEqual(provider.model_key(), "replacement-probe")
        self.assertEqual(provider.model_key("https://another.example/v1"), "")
        store.value = ""
        with self.assertRaises(KeychainError):
            provider.voice_key()

    def test_optional_model_key_is_empty_when_helper_is_unavailable(self):
        class Unavailable:
            def get(self, service, account):
                raise KeychainError("The bundled credential helper is unavailable")

        provider = SecretProvider({"model": "app.model", "voice": "app.voice"},
                                  keychain=Unavailable(), model_endpoint="")
        self.assertEqual(provider.model_key("https://model.example/v1"), "")
        with self.assertRaises(KeychainError):
            provider.voice_key()

    def test_authorization_required_is_visible_and_never_retried(self):
        calls = []

        def run(arguments, **options):
            calls.append(json.loads(options["input"]))
            response = {"state": "error", "code": "authorization_required",
                        "message": "Use Allow Keychain access in Connections."}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(response), "")

        with tempfile.NamedTemporaryFile() as executable:
            with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": executable.name}):
                store = Keychain(runner=run)
                with self.assertRaises(KeychainAuthorizationRequired):
                    store.get("app.probe")
                self.assertEqual(len(calls), 1)
                grant = GrantStore("app.probe", keychain=store)
                self.assertEqual(grant.report(0)["state"], "authorization_required")
        self.assertEqual([row["operation"] for row in calls], ["get", "get"])


if __name__ == "__main__":
    unittest.main()

"""Safe connection diagnostics and bridge control dispatch."""

import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.account_oauth import OAuthError
from goblin_core.host import GoblinHost
from goblin_bridge import Bridge, SLOW_METHODS


class ConnectionControlTests(unittest.TestCase):
    def test_account_and_voice_checks_use_the_slow_lane(self):
        for method in ("account/list", "account/status", "account/start",
                       "account/cancel", "account/check", "voice/check"):
            with self.subTest(method=method):
                self.assertIn(method, SLOW_METHODS)

    def test_bridge_dispatches_the_selected_provider(self):
        host = SimpleNamespace(account_list=Mock(return_value={"accounts": []}),
                               account_cancel=Mock(return_value={"status": "cancelled"}),
                               account_check=Mock(return_value={"ok": False}),
                               voice_check=Mock(return_value={"ok": False}))
        bridge = Bridge.__new__(Bridge)
        bridge.host = host
        self.assertEqual(bridge.handle("account/list", {}), {"accounts": []})
        bridge.handle("account/cancel", {"provider": "openai-codex"})
        host.account_cancel.assert_called_once_with("openai-codex")
        bridge.handle("account/check", {"provider": "xai"})
        host.account_check.assert_called_once_with("xai")
        bridge.handle("voice/check", {})
        host.voice_check.assert_called_once_with()

    def test_model_catalog_normalizes_both_provider_shapes(self):
        rows = GoblinHost._account_models({"models": [
            {"slug": "account-model", "display_name": "Account model",
             "supported_in_api": False},
            {"slug": "account-model"}]})
        self.assertEqual(rows, [{"id": "account-model", "name": "Account model"}])
        self.assertEqual(GoblinHost._account_models({"data": [{"id": "api-model"}]}),
                         [{"id": "api-model", "name": "api-model"}])
        with self.assertRaises(ValueError):
            GoblinHost._account_models({"data": [{"name": "Missing identifier"}]})

    def test_missing_account_does_not_fall_back_to_a_key(self):
        host = GoblinHost.__new__(GoblinHost)
        host._stopping = False
        host._account_checks_lock = threading.RLock()
        host._account_checks = {}
        host.accounts = SimpleNamespace(
            definition=lambda provider: {"model_base_url": "https://example.invalid",
                                         "models_path": "/models"},
            credential=Mock(side_effect=OAuthError("Sign in required")))
        host.secret_provider = SimpleNamespace(model_key=Mock())
        result = host.account_check("xai")
        self.assertFalse(result["ok"])
        self.assertIn("Sign in required", result["message"])
        host.secret_provider.model_key.assert_not_called()
        self.assertEqual(host._account_checks, {})

    def test_voice_check_mints_only_and_returns_no_credential(self):
        host = GoblinHost.__new__(GoblinHost)
        host._stopping = False
        host._config = SimpleNamespace(voice_auth="oauth")
        host.voice = SimpleNamespace(refresh_settings=Mock(), start=Mock())
        host.voice_client = SimpleNamespace(
            mint=Mock(return_value={"value": "fixture-ephemeral", "expires_at": 123}),
            _secret="fixture-bearer")
        host.session = SimpleNamespace(log=SimpleNamespace(add_secrets=Mock()))
        result = host.voice_check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["auth"], "oauth")
        self.assertNotIn("fixture-ephemeral", json.dumps(result))
        self.assertNotIn("fixture-bearer", json.dumps(result))
        host.voice.start.assert_not_called()
        host.voice_client.mint.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

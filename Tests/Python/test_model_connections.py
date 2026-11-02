"""Independent account and custom endpoint bindings for chat and compression."""

import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import Config, ConfigError, default_settings, normalize_settings
from goblin_core.credentials import ResolvedCredential
from goblin_core.model_seat import ModelClientPool, ModelSeat
from goblin_core.session import GoblinSession


class ModelConnectionTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "product.json").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.values = default_settings(self.manifest)
        self.values["model"] = "chat-model"
        self.values["model_base_url"] = "http://127.0.0.1:9/v1"
        self.values["world_view"] = "Preserve the current work."

    def config(self):
        values = normalize_settings(self.values, self.manifest, self.home)
        return Config(self.home, self.manifest, values)

    def test_legacy_settings_keep_custom_route(self):
        self.values.pop("model_connection")
        config = self.config()
        self.assertEqual(config.model_connection, "custom")
        self.assertEqual(config.compactor.connection, "")
        self.assertEqual(config.model, "chat-model")
        self.assertEqual(config.model_base_url, "http://127.0.0.1:9/v1")

    def test_each_account_selection_preserves_custom_settings(self):
        for provider in self.manifest["oauth"]:
            with self.subTest(provider=provider):
                self.values["model_connection"] = provider
                config = self.config()
                self.assertEqual(config.model_connection, provider)
                self.assertEqual(config.model, "chat-model")
                self.assertEqual(config.model_base_url, "http://127.0.0.1:9/v1")
                self.assertEqual(config.world_view, "Preserve the current work.")

    def test_compression_account_does_not_replace_chat(self):
        for provider in self.manifest["oauth"]:
            with self.subTest(provider=provider):
                self.values["advanced"] = {"compactor": {
                    "connection": provider, "model": "summary-model", "workers": 3}}
                config = self.config()
                self.assertEqual(config.model_connection, "custom")
                self.assertEqual(config.model, "chat-model")
                self.assertEqual(config.compactor.connection, provider)
                self.assertEqual(config.compactor.model, "summary-model")
                self.assertEqual(config.compactor.workers, 3)

    def test_unknown_connection_and_partial_compression_are_rejected(self):
        self.values["model_connection"] = "not-configured"
        with self.assertRaises(ConfigError):
            self.config()
        self.values["model_connection"] = "custom"
        self.values["advanced"] = {"compactor": {"connection": "xai"}}
        with self.assertRaisesRegex(
                ConfigError, "requires advanced.compactor.account_models.xai"):
            self.config()

    def host(self):
        from goblin_core.host import GoblinHost
        host = GoblinHost.__new__(GoblinHost)
        host._config = self.config()
        host._backend_cache = None
        host._backend_lock = threading.RLock()
        host._compactor_clients = ModelClientPool(host._build_backend)
        host.accounts = SimpleNamespace(
            definition=lambda provider: self.manifest["oauth"][provider],
            credential=lambda provider: ResolvedCredential("fixture-access", "oauth"))
        host.secret_provider = SimpleNamespace(model_key=lambda **kwargs: "")
        return host

    def test_inherited_binding_shares_the_actual_backend(self):
        host = self.host()
        self.assertIs(host._backend(), host._compaction_backend())
        self.assertEqual(host._backend().binding_key[0], "custom")

    def test_account_readiness_uses_the_account_binding(self):
        self.values["model_connection"] = "xai"
        self.values["model_base_url"] = ""
        self.values["api_kind"] = "cli_worker"
        self.values["account_models"] = {"xai": {"model": "grok-4"}}
        host = self.host()
        host._event_lock = threading.RLock()
        host._workspace_issue = ""
        host._settings_issue = ""
        self.assertEqual(host.setup_issue(), "")
        with patch.object(host.secret_provider, "model_key") as key:
            self.assertEqual(host._register_available_model_key(), "")
            key.assert_not_called()

    def test_account_compression_uses_its_own_transport_and_auth(self):
        for provider in self.manifest["oauth"]:
            with self.subTest(provider=provider):
                self.values["advanced"] = {"compactor": {
                    "connection": provider, "model": "summary-model"}}
                host = self.host()
                foreground = host._backend()
                background = host._compaction_backend()
                definition = self.manifest["oauth"][provider]
                self.assertIsNot(foreground, background)
                self.assertEqual(foreground.model, "chat-model")
                self.assertEqual(background.base_url, definition["model_base_url"])
                self.assertEqual(background.api_kind, definition["api_kind"])
                self.assertEqual(background.credential_provider().source, "oauth")
                self.assertIsNone(background.key_provider)

    def test_custom_compression_keys_are_endpoint_scoped(self):
        self.values["advanced"] = {"compactor": {
            "connection": "custom", "model": "summary-model",
            "model_base_url": "http://127.0.0.1:11/v1"}}
        host = self.host()
        with patch.object(host, "_model_key", return_value="") as lookup:
            host._backend().key_provider()
            self.assertEqual(lookup.call_args.args, ("http://127.0.0.1:9/v1",))
            host._compaction_backend().key_provider()
            self.assertEqual(lookup.call_args.args, ("http://127.0.0.1:11/v1",))

    def test_compression_uses_its_saved_reasoning_defaults(self):
        self.values["reasoning"] = "high"
        self.values["thinking_budget"] = 1024
        self.values["advanced"] = {"compactor": {
            "connection": "openai-codex",
            "account_models": {"openai-codex": {
                "model": "summary-model", "reasoning": "low", "thinking_budget": 256}},
        }}
        host = self.host()
        foreground = host._backend()
        background = host._compaction_backend()
        self.assertEqual(foreground.parameters["reasoning_effort"], "high")
        self.assertEqual(foreground.parameters["thinking_budget"], 1024)
        self.assertEqual(background.parameters["reasoning_effort"], "low")
        self.assertEqual(background.parameters["thinking_budget"], 256)

    def test_distinct_account_route_uses_exclusive_clients(self):
        self.values["advanced"] = {"compactor": {
            "connection": "openai-codex", "model": "summary-model"}}
        host = self.host()
        backend = host._compaction_backend()
        seat = ModelSeat()
        with host._compactor_client_lease(backend, seat, None) as (first, first_seat):
            with host._compactor_client_lease(backend, seat, None) as (second, second_seat):
                self.assertIsNot(first, second)
                self.assertIsNot(first._transport, second._transport)
                self.assertEqual(first.binding_key, backend.binding_key)
                self.assertIsNone(first_seat)
                self.assertIsNone(second_seat)

    def test_inherited_custom_route_uses_independent_background_transports(self):
        host = self.host()
        backend = host._backend()
        seat = ModelSeat()
        with seat.use(object(), lambda: None):
            with host._compactor_client_lease(backend, seat, None) as (first, first_seat):
                with host._compactor_client_lease(backend, seat, None) as (second, second_seat):
                    self.assertIsNot(first, backend)
                    self.assertIsNot(second, first)
                    self.assertIsNot(first._transport, backend._transport)
                    self.assertIsNot(second._transport, first._transport)
                    self.assertEqual(first.binding_key, backend.binding_key)
                    self.assertIsNone(first_seat)
                    self.assertIsNone(second_seat)
                    with patch.object(backend, "cancel") as foreground_cancel:
                        first.cancel()
                        foreground_cancel.assert_not_called()

    def test_shared_route_leases_an_independent_compactor_client(self):
        host = self.host()
        session = GoblinSession.__new__(GoblinSession)
        session.model_seat = ModelSeat()
        session._compactor_client_lease = host._compactor_client_lease
        backend = host._backend()
        with session._lease_compactor_client(backend, session.model_seat, None) as (client, selected):
            self.assertIsNot(client, backend)
            self.assertIsNone(selected)

    def test_background_concurrency_uses_configured_workers_for_every_route(self):
        session = GoblinSession.__new__(GoblinSession)
        session._compactor_client_lease = self.host()._compactor_client_lease
        self.assertEqual(session._compactor_client_count(self.config()), self.config().compactor.workers)
        self.values["advanced"] = {"compactor": {
            "connection": "openai-codex", "model": "summary-model", "workers": 2}}
        self.assertEqual(session._compactor_client_count(self.config()), 2)
        self.values["model_connection"] = "openai-codex"
        self.values["account_models"] = {"openai-codex": {"model": "summary-model"}}
        self.assertEqual(session._compactor_client_count(self.config()), 2)


if __name__ == "__main__":
    unittest.main()

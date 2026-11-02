"""Endpoint-bound model credentials, legacy adoption, and OAuth resolution.

No real Keychain item and no real credential is used. Every store here is an
in-memory fake, and every secret is a probe string.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.account_oauth import OAuthError
from goblin_core.credentials import (CredentialError, CredentialResolver,
                                     ResolvedCredential, redact_secret)
from goblin_core.keychain import (GrantStore, Keychain, KeychainError,
                                  LEGACY_MODEL_ACCOUNT, MODEL_MIGRATION_ACCOUNT,
                                  SecretProvider, canonical_model_endpoint)

SERVICE = "app.model"
ENDPOINT_A = "https://provider-a.example/v1"
ENDPOINT_B = "https://provider-b.example/v1"


class FakeKeychain:
    """Same call shapes as goblin_core.keychain.Keychain."""

    def __init__(self):
        self.items = {}
        self.get_calls = []
        self.put_calls = []
        self.delete_calls = []
        self.exists_calls = []

    def get(self, service, account):
        self.get_calls.append((service, account))
        value = self.items.get((service, account), "")
        if not value:
            return "", "missing"
        return value, "available"

    def put(self, service, value, account="default"):
        self.put_calls.append((service, account))
        self.items[(service, account)] = value

    def delete(self, service, account="default"):
        self.delete_calls.append((service, account))
        self.items.pop((service, account), None)

    def exists(self, service, account="default"):
        self.exists_calls.append((service, account))
        if (service, account) in self.items:
            return "found"
        return "missing"


class ReadbackCorruptingKeychain(FakeKeychain):
    """Writes a different value than it was given for the endpoint account."""

    def __init__(self, corrupt_account):
        super().__init__()
        self.corrupt_account = corrupt_account

    def put(self, service, value, account="default"):
        if account == self.corrupt_account:
            super().put(service, "corrupted-probe", account)
            return
        super().put(service, value, account)


class UnreadableLegacyKeychain(FakeKeychain):
    def get(self, service, account):
        if account == LEGACY_MODEL_ACCOUNT:
            raise KeychainError("Keychain read failed (probe)")
        return super().get(service, account)


class CanonicalEndpointTests(unittest.TestCase):
    def test_trailing_slashes_and_whitespace_share_one_account(self):
        self.assertEqual(canonical_model_endpoint("  " + ENDPOINT_A + "//  "), ENDPOINT_A)
        self.assertEqual(canonical_model_endpoint(ENDPOINT_A), ENDPOINT_A)

    def test_unusable_endpoint_text_has_no_account(self):
        self.assertEqual(canonical_model_endpoint(""), "")
        self.assertEqual(canonical_model_endpoint("https://"), "")
        self.assertEqual(canonical_model_endpoint("provider-a.example/v1"), "")
        self.assertEqual(canonical_model_endpoint(None), "")


class ModelKeyBindingTests(unittest.TestCase):
    def test_key_for_one_endpoint_is_not_offered_to_another(self):
        store = FakeKeychain()
        store.put(SERVICE, "key-a-probe", ENDPOINT_A)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_key(), "key-a-probe")
        self.assertEqual(provider.model_key(ENDPOINT_B), "")
        self.assertEqual(provider.model_key(ENDPOINT_A), "key-a-probe")
        accounts_read = [account for service, account in store.get_calls]
        self.assertNotIn(LEGACY_MODEL_ACCOUNT, accounts_read)

    def test_changed_endpoint_is_never_sent_the_previous_key(self):
        store = FakeKeychain()
        store.put(SERVICE, "key-a-probe", ENDPOINT_A)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_key(ENDPOINT_B), "")
        self.assertNotIn((SERVICE, ENDPOINT_B), store.items)
        self.assertEqual(provider.model_key(ENDPOINT_A), "key-a-probe")

    def test_replaced_key_is_read_on_the_next_request(self):
        store = FakeKeychain()
        store.put(SERVICE, "first-probe", ENDPOINT_A)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_key(), "first-probe")
        store.put(SERVICE, "replacement-probe", ENDPOINT_A)
        self.assertEqual(provider.model_key(), "replacement-probe")

    def test_missing_endpoint_reads_no_account(self):
        store = FakeKeychain()
        store.put(SERVICE, "legacy-probe", LEGACY_MODEL_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store)
        self.assertEqual(provider.model_key(), "")
        self.assertEqual(store.get_calls, [])


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_default_is_migrated_once_and_removed(self):
        store = FakeKeychain()
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "migrated")
        self.assertEqual(store.items[(SERVICE, ENDPOINT_A)], "legacy-key-probe")
        self.assertNotIn((SERVICE, LEGACY_MODEL_ACCOUNT), store.items)
        receipt = json.loads(store.items[(SERVICE, MODEL_MIGRATION_ACCOUNT)])
        self.assertEqual(receipt["endpoint"], ENDPOINT_A)
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(provider.model_key(), "legacy-key-probe")

    def test_legacy_default_cannot_be_rebound_after_endpoint_change(self):
        store = FakeKeychain()
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        first = SecretProvider({"model": SERVICE}, keychain=store,
                               model_endpoint=ENDPOINT_A)
        self.assertEqual(first.model_migration, "migrated")
        # Simulate an old build writing the single default item again.
        store.put(SERVICE, "old-build-probe", LEGACY_MODEL_ACCOUNT)
        second = SecretProvider({"model": SERVICE}, keychain=store,
                                model_endpoint=ENDPOINT_B)
        self.assertEqual(second.model_migration, "already-migrated")
        self.assertNotIn((SERVICE, ENDPOINT_B), store.items)
        self.assertEqual(second.model_key(), "")
        # Relaunching on the pinned endpoint must not overwrite its key either.
        third = SecretProvider({"model": SERVICE}, keychain=store,
                               model_endpoint=ENDPOINT_A)
        self.assertEqual(third.model_migration, "already-migrated")
        self.assertEqual(store.items[(SERVICE, ENDPOINT_A)], "legacy-key-probe")
        self.assertEqual(store.items[(SERVICE, LEGACY_MODEL_ACCOUNT)], "old-build-probe")

    def test_existing_endpoint_key_wins_over_a_legacy_item(self):
        store = FakeKeychain()
        store.put(SERVICE, "newer-key-probe", ENDPOINT_A)
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "kept-existing")
        self.assertEqual(store.items[(SERVICE, ENDPOINT_A)], "newer-key-probe")
        self.assertEqual(provider.model_key(), "newer-key-probe")

    def test_failed_readback_keeps_the_legacy_key(self):
        store = ReadbackCorruptingKeychain(ENDPOINT_A)
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "copy-unverified")
        self.assertEqual(store.items[(SERVICE, LEGACY_MODEL_ACCOUNT)], "legacy-key-probe")
        self.assertNotIn((SERVICE, ENDPOINT_A), store.items)
        self.assertEqual(provider.model_key(), "")
        receipt = json.loads(store.items[(SERVICE, MODEL_MIGRATION_ACCOUNT)])
        self.assertEqual(receipt["state"], "pending")

    def test_unreadable_legacy_key_is_not_deleted(self):
        store = UnreadableLegacyKeychain()
        store.items[(SERVICE, LEGACY_MODEL_ACCOUNT)] = "legacy-key-probe"
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "unavailable")
        self.assertEqual(store.items[(SERVICE, LEGACY_MODEL_ACCOUNT)], "legacy-key-probe")

    def test_pending_receipt_completes_the_copy_after_a_crash(self):
        store = FakeKeychain()
        store.put(SERVICE, "legacy-key-probe", ENDPOINT_A)
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        store.put(SERVICE, json.dumps({"version": 1, "endpoint": ENDPOINT_A,
                                       "state": "pending"}), MODEL_MIGRATION_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "already-migrated")
        self.assertNotIn((SERVICE, LEGACY_MODEL_ACCOUNT), store.items)
        receipt = json.loads(store.items[(SERVICE, MODEL_MIGRATION_ACCOUNT)])
        self.assertEqual(receipt["state"], "complete")
        self.assertEqual(provider.model_key(), "legacy-key-probe")

    def test_pending_receipt_keeps_a_different_legacy_secret(self):
        store = FakeKeychain()
        store.put(SERVICE, "newer-key-probe", ENDPOINT_A)
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        store.put(SERVICE, json.dumps({"version": 1, "endpoint": ENDPOINT_A,
                                       "state": "pending"}), MODEL_MIGRATION_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "already-migrated")
        self.assertEqual(store.items[(SERVICE, ENDPOINT_A)], "newer-key-probe")
        self.assertEqual(store.items[(SERVICE, LEGACY_MODEL_ACCOUNT)], "legacy-key-probe")

    def test_invalid_receipt_blocks_migration(self):
        store = FakeKeychain()
        store.put(SERVICE, "legacy-key-probe", LEGACY_MODEL_ACCOUNT)
        store.put(SERVICE, '{"endpoint": "not-a-url", "state": "complete", "version": 1}',
                  MODEL_MIGRATION_ACCOUNT)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        self.assertEqual(provider.model_migration, "receipt-invalid")
        self.assertNotIn((SERVICE, ENDPOINT_A), store.items)

    def test_bare_process_never_touches_app_owned_keychain_items(self):
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": ""}):
            keychain = Keychain(runner=run)
            provider = SecretProvider({"model": SERVICE, "voice": SERVICE},
                                      keychain=keychain, model_endpoint=ENDPOINT_A)
            self.assertEqual(provider.model_migration, "app-bridge-unavailable")
            self.assertEqual(calls, [])
            # An optional model key is empty. A required voice key still
            # raises, so a bare process cannot use the app's credential path.
            self.assertEqual(provider.model_key(), "")
            self.assertEqual(calls, [])
            with self.assertRaises(KeychainError):
                provider.voice_key()
            self.assertEqual(calls, [])


class ModelKeyStateTests(unittest.TestCase):
    def test_status_does_not_read_the_secret(self):
        store = FakeKeychain()
        store.put(SERVICE, "key-a-probe", ENDPOINT_A)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        store.get_calls.clear()
        self.assertEqual(provider.model_key_status(), "found")
        self.assertEqual(store.get_calls, [])
        provider.delete_model_key()
        self.assertEqual(provider.model_key_status(), "missing")
        self.assertEqual(store.get_calls, [])

    def test_remove_is_scoped_to_the_requested_endpoint(self):
        store = FakeKeychain()
        store.put(SERVICE, "key-a-probe", ENDPOINT_A)
        store.put(SERVICE, "key-b-probe", ENDPOINT_B)
        provider = SecretProvider({"model": SERVICE}, keychain=store,
                                  model_endpoint=ENDPOINT_A)
        provider.delete_model_key(ENDPOINT_A)
        self.assertEqual(provider.model_key(ENDPOINT_A), "")
        self.assertEqual(provider.model_key(ENDPOINT_B), "key-b-probe")


class KeychainPresenceTests(unittest.TestCase):
    def test_missing_helper_never_falls_back_to_system_security(self):
        calls = []

        def run(arguments, **options):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 44, "", "")

        with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": ""}):
            keychain = Keychain(runner=run)
            self.assertEqual(keychain.exists("app.model", ENDPOINT_A), "unavailable")
        self.assertEqual(calls, [])

    def test_native_presence_check_uses_the_exists_operation(self):
        calls = []

        def run(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"state": "found"}), "")

        import tempfile
        with tempfile.NamedTemporaryFile() as executable:
            with patch.dict(os.environ, {"GOBLIN_KEYCHAIN_HELPER": executable.name}):
                keychain = Keychain(runner=run)
                self.assertEqual(keychain.exists("app.model", ENDPOINT_A), "found")
        arguments, options = calls[0]
        self.assertEqual(len(arguments), 1)
        request = json.loads(options["input"])
        self.assertEqual(request["operation"], "exists")
        self.assertNotIn("value", request)


class FakeGrantStore:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.loads = 0

    def load(self):
        self.loads += 1
        if self.error is not None:
            raise self.error
        return self.payload


class CredentialResolverOAuthTests(unittest.TestCase):
    def test_env_reference_still_resolves_without_an_oauth_store(self):
        resolver = CredentialResolver({"probe_key": {"env": "GOBLIN_PROBE_KEY"}},
                                      environ={"GOBLIN_PROBE_KEY": "probe-env-value"})
        resolved = resolver.resolve("probe_key")
        self.assertEqual(resolved.value, "probe-env-value")
        self.assertEqual(resolved.source, "env")

    def test_oauth_reference_without_an_app_owned_store_is_rejected(self):
        with self.assertRaises(ValueError):
            CredentialResolver({"xai_grant": {"oauth": "xai"}})

    def test_oauth_reference_resolves_through_the_supplied_store(self):
        payload = {
            "access_token": "probe-access-token",
            "refresh_token": "probe-refresh-token",
            "expires_at": int(time.time()) + 3600,
        }
        store = FakeGrantStore(payload)
        resolver = CredentialResolver({"xai_grant": {"oauth": "xai"}},
                                      oauth_stores={"xai": store})
        resolved = resolver.resolve("xai_grant")
        self.assertEqual(resolved.value, "probe-access-token")
        self.assertEqual(resolved.source, "oauth")
        self.assertEqual(store.loads, 1)

    def test_app_owned_grant_store_is_the_opening_path(self):
        keychain = FakeKeychain()
        grant = GrantStore("app.oauth.xai", keychain=keychain)
        grant.save({
            "access_token": "probe-access-token",
            "refresh_token": "probe-refresh-token",
            "expires_at": int(time.time()) + 3600,
        })
        resolver = CredentialResolver({"xai_grant": {"oauth": "xai"}},
                                      oauth_stores={"xai": grant})
        resolved = resolver.resolve("xai_grant")
        self.assertEqual(resolved.value, "probe-access-token")
        self.assertEqual(resolved.source, "oauth")

    def test_rejected_grant_becomes_a_credential_error(self):
        store = FakeGrantStore(error=OAuthError("Sign in required. Probe."))
        resolver = CredentialResolver({"xai_grant": {"oauth": "xai"}},
                                      oauth_stores={"xai": store})
        with self.assertRaisesRegex(CredentialError, "Sign in required"):
            resolver.resolve("xai_grant")

    def test_unknown_oauth_store_name_is_rejected(self):
        with self.assertRaises(ValueError):
            CredentialResolver(oauth_stores={"some-other-client": FakeGrantStore()})

    def test_resolved_credential_repr_hides_the_value(self):
        resolved = ResolvedCredential("probe-secret-value", "env")
        self.assertNotIn("probe-secret-value", repr(resolved))


class RedactionTests(unittest.TestCase):
    def test_resolved_key_is_removed_from_nested_provider_error_data(self):
        payload = {
            "error": "rejected probe-secret-value",
            "items": ["probe-secret-value", {"detail": "header probe-secret-value"}],
        }
        redacted = redact_secret(payload, "probe-secret-value")
        self.assertEqual(redacted["error"], "rejected [REDACTED]")
        self.assertEqual(redacted["items"][0], "[REDACTED]")
        self.assertEqual(redacted["items"][1]["detail"], "header [REDACTED]")


if __name__ == "__main__":
    unittest.main()

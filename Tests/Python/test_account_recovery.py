"""Account identity and cancelled-attempt recovery regressions."""

import threading
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_account import (
    MANIFEST, MemoryGrantKeychain, MemoryGrantStore, jwt,
    XAI_TOKEN_URL,
)
from goblin_core import account_oauth


class AccountRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def test_data_home_aliases_share_one_keychain_refresh_lock(self):
        first = account_oauth.AccountRegistry(self.home / "one", manifest=MANIFEST,
                                              keychain=MemoryGrantKeychain())
        second = account_oauth.AccountRegistry(self.home / "two", manifest=MANIFEST,
                                               keychain=MemoryGrantKeychain())
        for provider in MANIFEST["oauth"]:
            self.assertEqual(first.stores[provider].service, second.stores[provider].service)
            self.assertEqual(first.stores[provider].lock_path, second.stores[provider].lock_path)

    def test_openai_account_headers_come_from_the_id_token(self):
        store = MemoryGrantStore()
        identity = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"}})
        account_oauth._save_tokens(store, "openai-codex", {
            "access_token": "opaque-access", "refresh_token": "fixture-refresh",
            "id_token": identity, "expires_in": 600,
        }, "", "https://auth.openai.com/oauth/token", 1000)
        self.assertEqual(store.load()["account_id"], "fixture-account")
        self.assertNotIn("id_token", store.load())
        account_oauth._save_tokens(store, "openai-codex", {
            "access_token": "rotated-opaque-access", "expires_in": 600,
        }, "fixture-refresh", "https://auth.openai.com/oauth/token", 1100)
        self.assertEqual(store.load()["account_id"], "fixture-account")

    def test_catalog_supplies_the_provider_client_version(self):
        query = urlsplit(MANIFEST["oauth"]["openai-codex"]["models_path"]).query
        self.assertTrue(parse_qs(query).get("client_version"))

    def test_restart_does_not_reactivate_a_cancelled_poll(self):
        old_sleep = threading.Event()
        new_sleep = threading.Event()
        release_old = threading.Event()
        release_new = threading.Event()
        calls = []
        sleep_count = 0
        device_count = 0

        def sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                old_sleep.set()
                release_old.wait(2)
            else:
                new_sleep.set()
                release_new.wait(2)

        def post(url, body, headers):
            nonlocal device_count
            if url == MANIFEST["oauth"]["xai"]["device_url"]:
                device_count += 1
                return 200, {"device_code": "fixture-device-" + str(device_count),
                             "user_code": "fixture-code", "verification_uri": "https://auth.x.ai/device",
                             "interval": 1, "expires_in": 600}
            calls.append(parse_qs(body.decode())["device_code"][0])
            return 400, {"error": "authorization_pending"}

        registry = account_oauth.AccountRegistry(
            self.home, manifest=MANIFEST, keychain=MemoryGrantKeychain(), poster=post,
            getter=lambda url, headers: {"token_endpoint": XAI_TOKEN_URL}, sleep=sleep,
            lock_root=self.home / "locks")
        try:
            registry.start("xai")
            self.assertTrue(old_sleep.wait(1))
            old_thread = registry._logins["xai"]._thread
            registry.cancel("xai")
            registry.start("xai")
            self.assertTrue(new_sleep.wait(1))
            release_old.set()
            old_thread.join(1)
            self.assertFalse(old_thread.is_alive())
            self.assertNotIn("fixture-device-1", calls)
        finally:
            registry.cancel_all()
            release_old.set()
            release_new.set()
            registry._logins["xai"]._thread.join(1)


if __name__ == "__main__":
    unittest.main()

"""Own-home OpenAI and xAI account sign-in tests. No network, no real Keychain."""

import base64
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core import account_oauth
from goblin_core.config import default_settings, write_settings
from goblin_core.voice import VoiceClient

MANIFEST = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
XAI = MANIFEST["oauth"]["xai"]
OPENAI = MANIFEST["oauth"]["openai-codex"]
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
OPENAI_DEVICE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
OPENAI_POLL_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"


def jwt(claims):
    """A signed-looking JWT whose claims are readable without a key."""
    def part(payload):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return part({"alg": "none", "typ": "JWT"}) + "." + part(claims) + ".probe"


def no_tokens(value):
    if isinstance(value, dict):
        for key in ("access_token", "refresh_token", "id_token", "code_verifier"):
            if key in value:
                return False
        return all(no_tokens(item) for item in value.values())
    if isinstance(value, list):
        return all(no_tokens(item) for item in value)
    return True


class MemoryGrantKeychain:
    """The same service/value/account shape as keychain.Keychain, in memory."""

    def __init__(self):
        self.items = {}

    def get(self, service, account="default"):
        value = self.items.get((service, account), "")
        return value, ("available" if value else "missing")

    def put(self, service, value, account="default"):
        if not service or not isinstance(value, str) or not value:
            raise ValueError("invalid Keychain write")
        self.items[(service, account)] = value

    def delete(self, service, account="default"):
        self.items.pop((service, account), None)


class MemoryGrantStore:
    """A test fake for the exported access_token caller shape."""

    def __init__(self):
        self.tokens = None
        self.saved = 0
        self.lock = threading.RLock()

    def refresh_guard(self):
        return self.lock

    def load(self):
        return dict(self.tokens) if self.tokens else None

    def save(self, tokens):
        self.saved += 1
        self.tokens = dict(tokens)

    def delete(self):
        self.tokens = None

    def report(self, now):
        if not self.tokens:
            return {"state": "missing", "has_refresh": False, "expires_at": 0,
                    "status": "Not signed in"}
        access = self.tokens.get("access_token") or ""
        refresh = self.tokens.get("refresh_token") or ""
        expires = self.tokens.get("expires_at") or 0
        if not access or (expires and expires <= int(now)):
            return {"state": "expired", "has_refresh": bool(refresh), "expires_at": expires,
                    "status": "Access expired. Sign in again."}
        return {"state": "current", "has_refresh": bool(refresh), "expires_at": expires,
                "status": "Signed in"}


def no_network(url, body, headers):
    """Fail loudly if a test ever tries a real sign-in request."""
    raise AssertionError("test attempted a network sign-in request: " + url)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return None


def signed_in_status(registry, provider):
    """Wait for a provider to report a current grant, or fail the test."""
    def current():
        report = registry.status(provider)
        if report["grant"]["state"] == "current":
            return report
        return None
    report = wait_for(current)
    if report is None:
        raise AssertionError("provider did not reach a current grant: " + provider)
    return report


class AccountRegistryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self.keychain = MemoryGrantKeychain()

    def tearDown(self):
        self._temp.cleanup()

    def registry(self, **options):
        options.setdefault("manifest", MANIFEST)
        options.setdefault("keychain", self.keychain)
        options.setdefault("poster", no_network)
        options.setdefault("getter", no_network)
        options.setdefault("sleep", lambda seconds: None)
        options.setdefault("lock_root", self.home / "locks")
        return account_oauth.AccountRegistry(self.home, **options)

    def test_manifest_lists_both_providers_without_secrets(self):
        registry = self.registry()
        definitions = [registry.definition("xai"), registry.definition("openai-codex")]
        self.assertEqual([row["name"] for row in definitions], ["xAI", "OpenAI"])
        self.assertEqual(definitions[0]["api_kind"], "responses")
        self.assertEqual(definitions[0]["model_base_url"], "https://api.x.ai/v1")
        self.assertEqual(definitions[1]["api_kind"], "codex_responses")
        self.assertEqual(definitions[1]["model_base_url"],
                         "https://chatgpt.com/backend-api/codex")
        self.assertEqual(definitions[1]["keychain_service"],
                         "com.maverickkb.goblin.oauth.openai-codex")
        reports = registry.list()
        self.assertEqual([report["provider"] for report in reports], ["xai", "openai-codex"])
        self.assertTrue(no_tokens(reports))
        self.assertEqual(reports[0]["status"], "missing")
        self.assertEqual(json.loads(json.dumps(reports)), reports)

    def test_unknown_provider_is_a_safe_error(self):
        registry = self.registry()
        for call in (registry.status, registry.start, registry.cancel,
                     registry.definition, registry.credential):
            with self.assertRaises(account_oauth.OAuthError):
                call("some-other-client")

    def test_xai_device_flow_returns_code_then_signs_in(self):
        posts = []
        approval = threading.Event()

        def poster(url, body, headers):
            posts.append((url, parse_qs(body.decode("utf-8"))))
            if url == XAI["device_url"]:
                return 200, {"user_code": "ABCD-EFGH", "device_code": "device-1",
                             "verification_uri_complete":
                                 "https://auth.x.ai/device?code=ABCD-EFGH",
                             "interval": 1, "expires_in": 600}
            approval.wait(5)
            return 200, {"access_token": "own-access", "refresh_token": "own-refresh",
                         "expires_in": 3600}

        def getter(url, headers):
            return {"token_endpoint": XAI_TOKEN_URL}

        registry = self.registry(poster=poster, getter=getter, now=lambda: 1000.0)
        started = registry.start("xai")
        self.assertEqual(started["provider"], "xai")
        self.assertEqual(started["status"], "waiting")
        self.assertEqual(started["user_code"], "ABCD-EFGH")
        self.assertTrue(started["verification_url"].startswith("https://auth.x.ai/"))
        self.assertTrue(no_tokens(started))
        approval.set()
        status = signed_in_status(registry, "xai")
        self.assertEqual(status["status"], "signed_in")
        self.assertEqual(status["user_code"], "")
        self.assertEqual(status["verification_url"], "")
        self.assertEqual(registry.stores["xai"].load()["access_token"], "own-access")
        self.assertEqual(posts[0][1]["client_id"], [XAI["client_id"]])

    def test_openai_device_flow_exchanges_the_code_and_keeps_account_headers(self):
        account_token = jwt({
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123",
                                            "chatgpt_data_residency": "us"},
        })
        seen = []
        approval = threading.Event()

        def poster(url, body, headers):
            if url == OPENAI_DEVICE_URL:
                self.assertEqual(json.loads(body.decode("utf-8")),
                                 {"client_id": OPENAI["client_id"]})
                return 200, {"device_auth_id": "device-auth-1", "user_code": "WXYZ-1234",
                             "interval": "1"}
            if url == OPENAI_POLL_URL:
                self.assertEqual(json.loads(body.decode("utf-8")),
                                 {"device_auth_id": "device-auth-1", "user_code": "WXYZ-1234"})
                approval.wait(5)
                return 200, {"authorization_code": "auth-code", "code_challenge": "challenge",
                             "code_verifier": "verifier"}
            self.assertEqual(url, OPENAI_TOKEN_URL)
            seen.append(parse_qs(body.decode("utf-8")))
            return 200, {"access_token": account_token, "refresh_token": "openai-refresh",
                         "id_token": jwt({"sub": "probe"})}

        registry = self.registry(poster=poster)
        started = registry.start("openai-codex")
        self.assertEqual(started["status"], "waiting")
        self.assertEqual(started["user_code"], "WXYZ-1234")
        self.assertEqual(started["verification_url"], "https://auth.openai.com/codex/device")
        approval.set()
        status = signed_in_status(registry, "openai-codex")
        self.assertEqual(status["status"], "signed_in")
        self.assertEqual(seen[0]["grant_type"], ["authorization_code"])
        self.assertEqual(seen[0]["code"], ["auth-code"])
        self.assertEqual(seen[0]["client_id"], [OPENAI["client_id"]])
        self.assertEqual(seen[0]["redirect_uri"],
                         ["https://auth.openai.com/deviceauth/callback"])
        self.assertEqual(seen[0]["code_verifier"], ["verifier"])

        credential = registry.credential("openai-codex")
        self.assertEqual(credential.value, account_token)
        self.assertEqual(credential.source, "oauth")
        self.assertEqual(credential.headers["ChatGPT-Account-ID"], "acct-123")
        self.assertEqual(credential.headers["x-openai-internal-codex-residency"], "us")
        self.assertNotIn(account_token, repr(credential))

    def test_openai_poll_stays_pending_while_the_code_is_unapproved(self):
        polls = []
        approval = threading.Event()

        def poster(url, body, headers):
            if url == OPENAI_DEVICE_URL:
                return 200, {"device_auth_id": "device-auth-1", "user_code": "WXYZ-1234",
                             "interval": 1}
            if url == OPENAI_POLL_URL:
                polls.append(url)
                if len(polls) < 3:
                    return 403, {}
                approval.wait(5)
                return 200, {"authorization_code": "auth-code", "code_verifier": "verifier"}
            return 200, {"access_token": "openai-access", "refresh_token": "openai-refresh"}

        registry = self.registry(poster=poster, now=lambda: 1000.0)
        started = registry.start("openai-codex")
        self.assertEqual(started["status"], "waiting")
        self.assertEqual(registry.status("openai-codex")["status"], "waiting")
        approval.set()
        signed_in_status(registry, "openai-codex")
        self.assertEqual(len(polls), 3)

    def test_reconnect_keeps_the_old_grant_and_cancel_is_terminal(self):
        poll_gate = threading.Event()

        def poster(url, body, headers):
            if url == XAI["device_url"]:
                return 200, {"user_code": "NEW-CODE", "device_code": "device-2",
                             "verification_uri_complete":
                                 "https://auth.x.ai/device?code=NEW-CODE",
                             "interval": 1, "expires_in": 600}
            poll_gate.wait(5)
            return 200, {"error": "authorization_pending"}

        registry = self.registry(poster=poster, getter=lambda url, headers: {
            "token_endpoint": XAI_TOKEN_URL})
        registry.stores["xai"].save({
            "access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": int(time.time()) + 3600, "token_endpoint": XAI_TOKEN_URL})
        started = registry.start("xai")
        self.assertEqual(started["status"], "waiting")
        self.assertEqual(started["user_code"], "NEW-CODE")
        self.assertEqual(started["grant"]["state"], "current")
        again = registry.start("xai")
        self.assertEqual(again["user_code"], "NEW-CODE")

        cancelled = registry.cancel("xai")
        poll_gate.set()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["message"], "Sign-in cancelled.")
        self.assertEqual(cancelled["user_code"], "")
        self.assertEqual(cancelled["verification_url"], "")
        self.assertEqual(registry.stores["xai"].load()["access_token"], "old-access")
        self.assertEqual(registry.status("xai")["status"], "cancelled")

    def test_cancel_fences_a_late_approval_before_any_save(self):
        poll_started = threading.Event()
        approval = threading.Event()

        def poster(url, body, headers):
            if url == XAI["device_url"]:
                return 200, {"user_code": "LATE-CODE", "device_code": "device-3",
                             "verification_uri_complete":
                                 "https://auth.x.ai/device?code=LATE-CODE",
                             "interval": 1, "expires_in": 600}
            poll_started.set()
            approval.wait(5)
            return 200, {"access_token": "late-access", "refresh_token": "late-refresh",
                         "expires_in": 3600}

        registry = self.registry(poster=poster, getter=lambda url, headers: {
            "token_endpoint": XAI_TOKEN_URL}, now=lambda: 1000.0)
        started = registry.start("xai")
        self.assertEqual(started["status"], "waiting")
        self.assertTrue(poll_started.wait(5))
        registry.cancel("xai")
        approval.set()
        time.sleep(0.1)
        self.assertIsNone(registry.stores["xai"].load())
        self.assertEqual(registry.status("xai")["status"], "cancelled")

    def test_cold_start_waiting_status_from_another_process_is_not_active(self):
        status_path = self.home / "oauth" / "xai.json"
        status_path.parent.mkdir(parents=True)
        status_path.write_text(json.dumps({
            "status": "waiting", "attempt": "dead-attempt", "process": "other-process",
            "verification_url": "https://auth.x.ai/device?code=OLD-CODE",
            "user_code": "OLD-CODE", "message": "Approve this code in the browser.",
        }), encoding="utf-8")
        registry = self.registry()
        status = registry.status("xai")
        self.assertEqual(status["status"], "missing")
        self.assertEqual(status["user_code"], "")
        self.assertEqual(status["verification_url"], "")

    def test_missing_grant_is_an_explicit_error_with_no_api_key_fallback(self):
        registry = self.registry()
        with self.assertRaises(account_oauth.OAuthError) as caught:
            registry.credential("xai")
        self.assertIn("Sign in required", str(caught.exception))
        with self.assertRaises(account_oauth.OAuthError):
            registry.credential("openai-codex")

    def test_one_provider_grant_never_satisfies_the_other(self):
        self.registry().stores["xai"].save({
            "access_token": "xai-only", "refresh_token": "xai-refresh",
            "expires_at": int(time.time()) + 3600, "token_endpoint": XAI_TOKEN_URL})
        registry = self.registry()
        self.assertEqual(registry.credential("xai").value, "xai-only")
        with self.assertRaises(account_oauth.OAuthError):
            registry.credential("openai-codex")

    def test_supplied_manifest_defines_client_name_and_endpoint(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["oauth"]["xai"]["client_id"] = "probe-client"
        manifest["oauth"]["xai"]["name"] = "Probe xAI"
        manifest["oauth"]["xai"]["model_base_url"] = "https://probe.invalid/v1"
        posted = []

        def poster(url, body, headers):
            posted.append(parse_qs(body.decode("utf-8")))
            return 200, {"user_code": "PROBE", "device_code": "probe-device",
                         "verification_uri_complete": "https://auth.x.ai/device?code=PROBE",
                         "interval": 1, "expires_in": 600}

        registry = self.registry(manifest=manifest, poster=poster,
                                 getter=lambda url, headers: {"token_endpoint": XAI_TOKEN_URL})
        self.assertEqual(registry.definition("xai")["name"], "Probe xAI")
        self.assertEqual(registry.definition("xai")["model_base_url"],
                         "https://probe.invalid/v1")
        started = registry.start("xai")
        self.assertEqual(started["name"], "Probe xAI")
        self.assertEqual(posted[0]["client_id"], ["probe-client"])
        registry.cancel("xai")

    def test_refresh_rotates_once_and_serializes_across_registries(self):
        shared_home = self.home
        calls = []
        calls_lock = threading.Lock()

        def poster(url, body, headers):
            with calls_lock:
                calls.append(parse_qs(body.decode("utf-8")))
            time.sleep(0.05)
            return 200, {"access_token": "fresh", "refresh_token": "refresh-2",
                         "expires_in": 3600}

        first = self.registry(poster=poster)
        first.stores["xai"].save({
            "access_token": "stale", "refresh_token": "refresh-1",
            "expires_at": 1, "token_endpoint": XAI_TOKEN_URL})
        second = account_oauth.AccountRegistry(shared_home, manifest=MANIFEST,
                                               keychain=self.keychain, poster=poster,
                                               lock_root=self.home / "locks")
        self.assertEqual(first.stores["xai"].lock_path, second.stores["xai"].lock_path)
        results = []
        results_lock = threading.Lock()

        def resolve(registry):
            credential = registry.credential("xai")
            with results_lock:
                results.append(credential.value)

        threads = [threading.Thread(target=resolve, args=(first,)),
                   threading.Thread(target=resolve, args=(second,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["grant_type"], ["refresh_token"])
        self.assertEqual(calls[0]["refresh_token"], ["refresh-1"])
        self.assertEqual(sorted(results), ["fresh", "fresh"])
        self.assertEqual(first.stores["xai"].load()["refresh_token"], "refresh-2")
        self.assertTrue(first.stores["xai"].lock_path.exists())

    def test_refresh_rejection_is_explicit_and_keeps_no_partial_grant(self):
        def poster(url, body, headers):
            return 400, {"error": "invalid_grant"}

        registry = self.registry(poster=poster)
        registry.stores["xai"].save({
            "access_token": "stale", "refresh_token": "refresh-1",
            "expires_at": 1, "token_endpoint": XAI_TOKEN_URL})
        with self.assertRaises(account_oauth.OAuthError) as caught:
            registry.credential("xai")
        self.assertIn("Sign in required", str(caught.exception))
        self.assertEqual(registry.stores["xai"].load()["access_token"], "stale")

    def test_status_never_carries_a_token_and_clears_terminal_codes(self):
        def poster(url, body, headers):
            return 500, {}

        registry = self.registry(poster=poster, getter=lambda url, headers: {
            "token_endpoint": XAI_TOKEN_URL})
        with self.assertRaises(account_oauth.OAuthError):
            registry.start("xai")
        status = registry.status("xai")
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["user_code"], "")
        self.assertEqual(status["verification_url"], "")
        self.assertTrue(no_tokens(status))


class ExportedAccessTokenTests(unittest.TestCase):
    def test_refresh_failure_is_explicit_and_does_not_fall_back(self):
        store = MemoryGrantStore()
        store.tokens = {"access_token": "stale", "refresh_token": "refresh",
                        "expires_at": 1, "token_endpoint": XAI_TOKEN_URL}
        with self.assertRaises(account_oauth.OAuthError) as caught:
            account_oauth.access_token(
                store, poster=lambda url, body, headers: (400, {"error": "invalid_grant"}),
                now=lambda: 1000.0)
        self.assertIn("Sign in required", str(caught.exception))

    def test_refresh_saves_the_rotated_grant_once(self):
        store = MemoryGrantStore()
        store.tokens = {"access_token": "stale", "refresh_token": "refresh",
                        "expires_at": 1, "token_endpoint": XAI_TOKEN_URL}
        self.assertEqual(account_oauth.access_token(
            store, poster=lambda url, body, headers: (
                200, {"access_token": "fresh", "refresh_token": "refresh-2",
                      "expires_in": 3600}), now=lambda: 1000.0), "fresh")
        self.assertEqual(store.saved, 1)
        self.assertEqual(store.tokens["refresh_token"], "refresh-2")

    def test_store_definition_and_lock_are_honored_without_a_provider_argument(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            keychain = MemoryGrantKeychain()
            registry = account_oauth.AccountRegistry(home, manifest=MANIFEST, keychain=keychain)
            store = registry.stores["xai"]
            store.save({"access_token": "stale", "refresh_token": "refresh-1", "expires_at": 1,
                        "token_endpoint": XAI_TOKEN_URL})
            posted = []

            def poster(url, body, headers):
                posted.append(url)
                return 200, {"access_token": "fresh", "refresh_token": "refresh-2",
                             "expires_in": 3600}

            self.assertEqual(account_oauth.access_token(store, poster=poster), "fresh")
            self.assertEqual(posted, [XAI_TOKEN_URL])
            self.assertEqual(store.provider, "xai")
            self.assertIsNotNone(store.lock_path)

    def test_openai_refresh_ignores_an_untrusted_stored_token_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            keychain = MemoryGrantKeychain()
            registry = account_oauth.AccountRegistry(home, manifest=MANIFEST, keychain=keychain)
            store = registry.stores["openai-codex"]
            store.save({"access_token": "stale", "refresh_token": "refresh-1", "expires_at": 1,
                        "token_endpoint": XAI_TOKEN_URL})
            posted = []

            def poster(url, body, headers):
                posted.append(url)
                return 200, {"access_token": "fresh", "refresh_token": "refresh-2"}

            self.assertEqual(account_oauth.access_token(
                store, provider="openai-codex", poster=poster), "fresh")
            self.assertEqual(posted, [OPENAI_TOKEN_URL])

    def test_redirects_are_never_followed(self):
        handler = account_oauth._NoRedirect()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.invalid/token"))


class CrossProcessLockTests(unittest.TestCase):
    def test_refresh_guard_serializes_two_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            order = home / "order.log"
            script = textwrap.dedent(
                "import sys, time\n"
                "sys.path.insert(0, " + repr(str(ROOT / "Runtime")) + ")\n"
                "from pathlib import Path\n"
                "from goblin_core.keychain import GrantStore\n"
                "store = GrantStore('probe.service', lock_path=Path(" + repr(str(home))
                + ") / 'oauth' / 'probe.lock')\n"
                "with store.refresh_guard():\n"
                "    open(" + repr(str(order)) + ", 'a').write('start\\n')\n"
                "    time.sleep(0.2)\n"
                "    open(" + repr(str(order)) + ", 'a').write('end\\n')\n"
            )
            processes = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(2)]
            for process in processes:
                self.assertEqual(process.wait(15), 0)
            self.assertEqual(order.read_text().split(), ["start", "end", "start", "end"])


class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self, limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class VoiceAuthTests(unittest.TestCase):
    def test_oauth_voice_uses_the_registry_xai_grant_not_the_voice_api_key(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            manifest = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
            values = default_settings(manifest)
            values["workspace"] = str(home / "workspace")
            config = write_settings(home, ROOT / "product.json", values)
            registry = account_oauth.AccountRegistry(home, manifest=manifest,
                                                     keychain=MemoryGrantKeychain())
            registry.stores["xai"].save({
                "access_token": "own-access", "refresh_token": "own-refresh",
                "expires_at": 2000, "token_endpoint": XAI_TOKEN_URL})
            seen = {}

            def opener(request, timeout=None):
                seen["authorization"] = request.get_header("Authorization")
                return _FakeResponse(json.dumps({"value": "client-token",
                                                 "expires_at": 1060}).encode())

            client = VoiceClient(config.voice, grant_store=registry.stores["xai"],
                                 opener=opener, clock=lambda: 1000.0)
            minted = client.mint()
            self.assertEqual(minted["value"], "client-token")
            self.assertEqual(seen["authorization"], "Bearer own-access")


if __name__ == "__main__":
    unittest.main()

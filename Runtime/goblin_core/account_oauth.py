"""Goblin-owned OpenAI and xAI account sign-in and refresh.

Both providers use a device-code flow owned by this app. Grants live in
Goblin's own Keychain items. This module never reads another client's
credential store, never returns a token to the bridge, and never falls back
to an API key when an account sign-in is missing or rejected.

One manifest row describes each provider: its public OAuth protocol data,
its inference base URL, adapter kind, model-list path, display name and the
Keychain service that holds its grant. No provider URL or client identifier
is written into this file.

Refresh is serialized through the grant store's lock, both inside this
process and across processes, and every rotation reloads the grant under
that lock before it posts, so two consumers never rotate one refresh chain.
"""

import base64
import json
import threading
import time
import uuid

from pathlib import Path
from urllib import error, request
from urllib.parse import urlencode, urlsplit

from .credentials import ResolvedCredential
from .keychain import GrantStore, KeychainError, grant_lock_path

DEFAULT_PROVIDER = "xai"
OPENAI_PROVIDER = "openai-codex"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "product.json"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PENDING = {"authorization_pending", "slow_down"}
CODE_LIFETIME_SECONDS = 900
REFRESH_SKEW_SECONDS = 120
RFC_DEVICE = "rfc_device_code"
CODEX_DEVICE = "codex_device_code"
PROTOCOLS = (RFC_DEVICE, CODEX_DEVICE)

_DEFAULT_DEFINITIONS = None
_DEFAULT_DEFINITIONS_LOCK = threading.Lock()



class OAuthError(RuntimeError):
    """A sign-in failure that is safe to show. It never carries token text."""


class _NoRedirect(request.HTTPRedirectHandler):
    """Never follow a redirect, so credentials stay on the trusted origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, body, headers, poster=None):
    poster = poster or _urllib_post
    return poster(url, body, headers)


def _urllib_post(url, body, headers):
    _require_https(url)
    req = request.Request(url, data=body, headers=headers, method="POST")
    opener = request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            raw = response.read()
            status = response.status
    except error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if not raw:
        return status, {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _urllib_get(url, headers):
    _require_https(url)
    req = request.Request(url, headers=headers, method="GET")
    opener = request.build_opener(_NoRedirect())
    with opener.open(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise OAuthError("Sign-in discovery did not return an object")
    return payload


def _require_https(url):
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or not parts.netloc:
        raise OAuthError("Sign-in endpoints must use HTTPS")


def _post_form(definition, url, fields, poster=None):
    _require_https(url)
    headers = {"User-Agent": definition["user_agent"],
               "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    return _post(url, urlencode(fields).encode("utf-8"), headers, poster)


def _post_json(definition, url, fields, poster=None):
    _require_https(url)
    headers = {"User-Agent": definition["user_agent"],
               "Content-Type": "application/json", "Accept": "application/json"}
    return _post(url, json.dumps(fields).encode("utf-8"), headers, poster)


def load_manifest(path=DEFAULT_MANIFEST_PATH):
    """Read a product manifest from disk."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OAuthError("The product manifest cannot be read") from None
    if not isinstance(payload, dict):
        raise OAuthError("The product manifest is not an object")
    return payload


def _row_text(row, key, provider):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OAuthError("Account provider " + provider + " is missing " + key)
    return value.strip()


def _row_service(row, services, provider):
    service_key = _row_text(row, "keychain_service", provider)
    service = services.get(service_key)
    if not isinstance(service, str) or not service.strip():
        raise OAuthError("Account provider " + provider + " has no Keychain service")
    return service.strip()


def _definition_row(provider, row, services):
    protocol = _row_text(row, "protocol", provider)
    if protocol not in PROTOCOLS:
        raise OAuthError("Account provider " + provider + " has an unknown protocol")
    definition = {
        "provider": provider,
        "name": _row_text(row, "name", provider),
        "protocol": protocol,
        "client_id": _row_text(row, "client_id", provider),
        "model_base_url": _row_text(row, "model_base_url", provider).rstrip("/"),
        "api_kind": _row_text(row, "api_kind", provider),
        "models_path": _row_text(row, "models_path", provider),
        "keychain_service": _row_service(row, services, provider),
    }
    if protocol == RFC_DEVICE:
        definition["scope"] = _row_text(row, "scope", provider)
        definition["device_url"] = _row_text(row, "device_url", provider)
        definition["discovery_url"] = _row_text(row, "discovery_url", provider)
        definition["token_url_prefix"] = _row_text(row, "token_url_prefix", provider)
        _require_https(definition["device_url"])
        _require_https(definition["discovery_url"])
    else:
        definition["issuer"] = _row_text(row, "issuer", provider).rstrip("/")
        definition["device_code_path"] = _row_text(row, "device_code_path", provider)
        definition["device_token_path"] = _row_text(row, "device_token_path", provider)
        definition["verification_path"] = _row_text(row, "verification_path", provider)
        definition["token_path"] = _row_text(row, "token_path", provider)
        definition["redirect_path"] = _row_text(row, "redirect_path", provider)
        _require_https(definition["issuer"])
    return definition


def provider_definitions(manifest):
    """Validate and describe every account provider in one manifest.

    The returned table keeps manifest order. Each row carries only public,
    non-secret data: no grant value is read here.
    """
    if not isinstance(manifest, dict):
        raise OAuthError("The product manifest is not an object")
    table = manifest.get("oauth")
    services = manifest.get("keychain_services")
    if not isinstance(table, dict) or not table:
        raise OAuthError("The product manifest lists no account providers")
    if not isinstance(services, dict):
        raise OAuthError("The product manifest lists no Keychain services")
    identity = []
    for field in ("name", "version"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise OAuthError("The product manifest is missing " + field)
        identity.append(value.strip())
    user_agent = "/".join(identity)
    definitions = {}
    for provider, row in table.items():
        if not isinstance(provider, str) or not provider.strip():
            raise OAuthError("Account provider names must be nonempty text")
        if not isinstance(row, dict):
            raise OAuthError("Account provider " + provider + " is not an object")
        definition = _definition_row(provider, row, services)
        definition["user_agent"] = user_agent
        definitions[provider] = definition
    return definitions


def _default_definitions():
    global _DEFAULT_DEFINITIONS
    with _DEFAULT_DEFINITIONS_LOCK:
        if _DEFAULT_DEFINITIONS is None:
            _DEFAULT_DEFINITIONS = provider_definitions(load_manifest())
        return _DEFAULT_DEFINITIONS


def _store_definition(grant_store, provider):
    """The manifest row a store was built with, or the installed manifest row."""
    definition = getattr(grant_store, "definition", None)
    if isinstance(definition, dict) and definition.get("provider") == provider:
        return definition
    definitions = _default_definitions()
    if provider not in definitions:
        raise OAuthError("Unknown account provider")
    return definitions[provider]


def _sign_in_message(definition, detail=""):
    name = str(definition.get("name") or "the provider")
    message = "Sign in required. "
    if detail:
        message += detail + " "
    return message + "Open Settings and sign in with " + name + "."


def _jwt_claims(token):
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _codex_account_fields(token):
    """Workspace identity headers the Codex backend derives from the grant.

    They stay inside the Keychain grant so no account detail reaches settings,
    logs, status reports or the bridge.
    """
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    account_id = ""
    residency = ""
    if isinstance(auth, dict):
        account_id = str(auth.get("chatgpt_account_id") or "").strip()
        residency = str(auth.get("chatgpt_data_residency")
                        or auth.get("chatgpt_compute_residency") or "").strip()
    if not account_id:
        account_id = str(claims.get("chatgpt_account_id") or "").strip()
    return {"account_id": account_id, "residency": residency}


def _interval_seconds(value, default=5):
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(60, max(1, seconds))


def _lifetime_seconds(value, default):
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(3600, max(60, seconds))


def _expiry_time(payload, access_token, now):
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
        return int(now) + expires_in
    claims = _jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, int) and not isinstance(exp, bool) and exp > 0:
        return exp
    return 0


def _usable_access_token(grant, now):
    if not isinstance(grant, dict):
        return ""
    access = str(grant.get("access_token") or "").strip()
    if not access:
        return ""
    expires_at = grant.get("expires_at") or 0
    if type(expires_at) is not int or expires_at <= 0:
        return access
    if expires_at > int(now) + REFRESH_SKEW_SECONDS:
        return access
    return ""


def _save_tokens(grant_store, provider, payload, refresh_fallback, token_endpoint, now):
    """Persist one grant. ``now`` is a timestamp, not a clock callable."""
    access = str(payload.get("access_token") or "").strip()
    refresh = str(payload.get("refresh_token") or refresh_fallback or "").strip()
    if not access or not refresh:
        raise OAuthError("Sign-in did not return a usable grant")
    grant = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": _expiry_time(payload, access, now),
        "token_endpoint": token_endpoint,
    }
    if provider == OPENAI_PROVIDER:
        fields = _codex_account_fields(payload.get("id_token"))
        access_fields = _codex_account_fields(access)
        previous = grant_store.load() if refresh_fallback else None
        for name in ("account_id", "residency"):
            if not fields[name]:
                fields[name] = access_fields[name]
            if not fields[name] and isinstance(previous, dict):
                fields[name] = str(previous.get(name) or "")
        grant["account_id"] = fields["account_id"]
        grant["residency"] = fields["residency"]
    grant_store.save(grant)


def _trusted_token_endpoint(definition, grant):
    """The token endpoint this grant may post to, or an explicit sign-in error."""
    if definition["protocol"] == CODEX_DEVICE:
        endpoint = definition["issuer"].rstrip("/") + definition["token_path"]
        _require_https(endpoint)
        return endpoint
    endpoint = str(grant.get("token_endpoint") or "").strip()
    prefix = definition["token_url_prefix"]
    if not endpoint.startswith(prefix):
        raise OAuthError(_sign_in_message(definition, "The saved sign-in cannot be refreshed."))
    _require_https(endpoint)
    return endpoint


def _discover_token_endpoint(definition, getter):
    headers = {"User-Agent": definition["user_agent"], "Accept": "application/json"}
    discovered = _get(definition["discovery_url"], headers, getter)
    endpoint = str(discovered.get("token_endpoint") or "").strip()
    if not endpoint.startswith(definition["token_url_prefix"]):
        raise OAuthError("Sign-in discovery did not return a trusted token endpoint.")
    _require_https(endpoint)
    return endpoint


def _get(url, headers, getter=None):
    getter = getter or _urllib_get
    _require_https(url)
    return getter(url, headers)


def _write_status(path, status, url="", code="", message="", attempt="", process="", now=None):
    payload = {
        "status": status,
        "verification_url": url,
        "user_code": code,
        "message": message,
        "attempt": attempt,
        "process": process,
        "updated_at": int(now() if now else time.time()),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def _read_status(path):
    empty = {"status": "missing", "verification_url": "", "user_code": "",
             "message": "", "attempt": "", "process": ""}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict):
        return empty
    return {
        "status": str(payload.get("status") or "missing"),
        "verification_url": str(payload.get("verification_url") or ""),
        "user_code": str(payload.get("user_code") or ""),
        "message": str(payload.get("message") or ""),
        "attempt": str(payload.get("attempt") or ""),
        "process": str(payload.get("process") or ""),
    }


def _rfc_device_request(definition, poster, getter):
    status, payload = _post_form(
        definition,
        definition["device_url"],
        {"client_id": definition["client_id"], "scope": definition["scope"]},
        poster,
    )
    if status != 200:
        raise OAuthError(definition["name"] + " sign-in code request failed (HTTP "
                         + str(status) + ").")
    user_code = str(payload.get("user_code") or "").strip()
    device_code = str(payload.get("device_code") or "").strip()
    if not user_code or not device_code:
        raise OAuthError(definition["name"] + " did not issue a sign-in code.")
    url = str(payload.get("verification_uri_complete")
              or payload.get("verification_uri") or "").strip()
    if not url:
        raise OAuthError(definition["name"] + " did not return a sign-in page.")
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": url,
        "interval": _interval_seconds(payload.get("interval")),
        "expires_in": _lifetime_seconds(payload.get("expires_in"), CODE_LIFETIME_SECONDS),
        "token_endpoint": _discover_token_endpoint(definition, getter),
    }


def _codex_device_request(definition, poster):
    issuer = definition["issuer"].rstrip("/")
    status, payload = _post_json(
        definition,
        issuer + definition["device_code_path"],
        {"client_id": definition["client_id"]},
        poster,
    )
    if status != 200:
        raise OAuthError(definition["name"] + " sign-in code request failed (HTTP "
                         + str(status) + ").")
    user_code = str(payload.get("user_code") or "").strip()
    device_auth_id = str(payload.get("device_auth_id") or "").strip()
    if not user_code or not device_auth_id:
        raise OAuthError("OpenAI did not issue a sign-in code.")
    return {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "verification_url": issuer + definition["verification_path"],
        "interval": _interval_seconds(payload.get("interval")),
        "expires_in": CODE_LIFETIME_SECONDS,
    }


def _request_device_code(definition, poster, getter):
    if definition["protocol"] == CODEX_DEVICE:
        return _codex_device_request(definition, poster)
    return _rfc_device_request(definition, poster, getter)


def _rfc_poll(definition, device, poster):
    status, payload = _post_form(
        definition,
        device["token_endpoint"],
        {"grant_type": DEVICE_GRANT, "client_id": definition["client_id"],
         "device_code": device["device_code"]},
        poster,
    )
    if status == 200 and str(payload.get("access_token") or "").strip():
        return "approved", payload
    error_code = str(payload.get("error") or "")
    if error_code == "slow_down":
        return "slow_down", {}
    if error_code in PENDING:
        return "pending", {}
    if status >= 500:
        return "pending", {}
    if error_code:
        return "failed", {}
    return "failed", {}


def _codex_poll(definition, device, poster):
    issuer = definition["issuer"].rstrip("/")
    status, payload = _post_json(
        definition,
        issuer + definition["device_token_path"],
        {"device_auth_id": device["device_auth_id"], "user_code": device["user_code"]},
        poster,
    )
    if status == 200:
        code = str(payload.get("authorization_code") or "").strip()
        verifier = str(payload.get("code_verifier") or "").strip()
        if not code or not verifier:
            return "failed", {}
        return "approved", payload
    if status in (403, 404) or status >= 500:
        return "pending", {}
    return "failed", {}


def _poll_device(definition, device, poster):
    if definition["protocol"] == CODEX_DEVICE:
        return _codex_poll(definition, device, poster)
    return _rfc_poll(definition, device, poster)


def _codex_exchange(definition, payload, poster):
    issuer = definition["issuer"].rstrip("/")
    status, body = _post_form(
        definition,
        issuer + definition["token_path"],
        {
            "grant_type": "authorization_code",
            "client_id": definition["client_id"],
            "code": str(payload.get("authorization_code") or ""),
            "redirect_uri": issuer + definition["redirect_path"],
            "code_verifier": str(payload.get("code_verifier") or ""),
        },
        poster,
    )
    if status != 200 or not str(body.get("access_token") or "").strip():
        raise OAuthError("OpenAI sign-in could not be completed. Start again.")
    return body


def _approval_tokens(definition, payload, poster):
    if definition["protocol"] == CODEX_DEVICE:
        return _codex_exchange(definition, payload, poster)
    return payload


def _token_endpoint_for(definition, device):
    if definition["protocol"] == CODEX_DEVICE:
        return definition["issuer"].rstrip("/") + definition["token_path"]
    return device["token_endpoint"]


class LoginManager:
    """One device sign-in attempt at a time for one provider."""

    def __init__(self, home, grant_store, provider=DEFAULT_PROVIDER, definition=None,
                 status_path=None, poster=None, getter=None, sleep=time.sleep,
                 now=time.time, process_token=""):
        self.home = Path(home)
        self.grant_store = grant_store
        self.provider = provider
        if isinstance(definition, dict):
            self.definition = definition
        else:
            self.definition = _store_definition(grant_store, provider)
        if self.definition.get("provider") != provider:
            raise OAuthError("Account provider does not match its definition")
        self.status_path = Path(status_path or (self.home / "oauth" / (provider + ".json")))
        self.poster = poster or _urllib_post
        self.getter = getter or _urllib_get
        self.sleep = sleep
        self.now = now
        self.process_token = process_token or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._attempt = ""
        self._ready = threading.Event()
        self._start_error = ""

    def _thread_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _local_status(self, saved):
        """A status this registry instance may report as its own live attempt."""
        if saved.get("process") != self.process_token:
            # A waiting file from an earlier or other process is not an active
            # login. Fall back to what the saved grant actually proves.
            return ""
        status = saved.get("status", "")
        if status == "waiting":
            live = self._attempt and saved.get("attempt") == self._attempt and self._thread_alive()
            return "waiting" if live else "interrupted"
        if status in ("cancelled", "error"):
            return status
        return ""

    def _report(self):
        try:
            grant = self.grant_store.report(self.now())
        except KeychainError:
            grant = {"state": "unavailable", "has_refresh": False, "expires_at": 0,
                     "status": "Sign-in state is unavailable"}
        saved = _read_status(self.status_path)
        local = self._local_status(saved)
        name = self.definition["name"]
        if grant.get("state") == "authorization_required":
            return self._build("keychain_required", "", "", grant["status"], grant)
        if local == "waiting":
            return self._build("waiting", saved["verification_url"], saved["user_code"],
                               saved["message"], grant)
        if local == "cancelled":
            return self._build("cancelled", "", "", "Sign-in cancelled.", grant)
        if local == "error":
            return self._build("error", "", "", saved["message"] or "Sign-in failed.", grant)
        if local == "interrupted":
            return self._build("interrupted", "", "",
                               "Sign-in was interrupted. Start again.", grant)
        if grant.get("state") == "current":
            return self._build("signed_in", "", "", "Signed in.", grant)
        if grant.get("state") == "expired":
            return self._build("sign_in_required", "", "",
                               "Sign in required. The saved " + name + " grant expired.", grant)
        if grant.get("state") == "unavailable":
            return self._build("unavailable", "", "", grant.get("status", ""), grant)
        return self._build("missing", "", "", "Not signed in.", grant)

    def _build(self, status, url, code, message, grant):
        return {
            "provider": self.provider,
            "name": self.definition["name"],
            "status": status,
            "verification_url": url,
            "user_code": code,
            "message": message,
            "grant": grant,
        }

    def status(self):
        with self._lock:
            return self._report()

    def start(self):
        """Begin a fresh device login. A current grant stays until approval."""
        with self._lock:
            if self._attempt and self._thread_alive():
                return self._report()
            attempt = uuid.uuid4().hex
            self._attempt = attempt
            cancelled = threading.Event()
            ready = threading.Event()
            self._cancel = cancelled
            self._ready = ready
            self._start_error = ""
            _write_status(self.status_path, "waiting", "", "", "Requesting a sign-in code.",
                          attempt, self.process_token, self.now)
            self._thread = threading.Thread(target=self._run, args=(attempt, cancelled),
                                            name="goblin-" + self.provider + "-login",
                                            daemon=True)
            self._thread.start()
        ready.wait(20)
        with self._lock:
            if self._start_error:
                message = self._start_error
                self._start_error = ""
                raise OAuthError(message)
            return self._report()

    def cancel(self):
        """Stop a pending attempt before any grant save can land."""
        with self._lock:
            if not self._attempt or not self._thread_alive():
                return self._report()
            self._attempt = ""
            self._cancel.set()
            _write_status(self.status_path, "cancelled", "", "", "Sign-in cancelled.",
                          "", self.process_token, self.now)
            self._ready.set()
            return self._report()

    def _wait(self, seconds, cancelled):
        if seconds > 0:
            if self.sleep is time.sleep:
                cancelled.wait(seconds)
            else:
                self.sleep(seconds)
        return not cancelled.is_set()

    def _fail(self, attempt, message):
        with self.grant_store.refresh_guard():
            with self._lock:
                if attempt != self._attempt:
                    return
                self._attempt = ""
                _write_status(self.status_path, "error", "", "", message, "",
                              self.process_token, self.now)
                if not self._ready.is_set():
                    self._start_error = message
                self._ready.set()

    def _run(self, attempt, cancelled):
        try:
            device = _request_device_code(self.definition, self.poster, self.getter)
        except OAuthError as exc:
            self._fail(attempt, str(exc))
            return
        except (OSError, ValueError):
            self._fail(attempt, self.definition["name"] + " did not issue a sign-in code.")
            return
        with self._lock:
            if attempt != self._attempt:
                return
            _write_status(self.status_path, "waiting", device["verification_url"],
                          device["user_code"], "Approve this code in the browser.",
                          attempt, self.process_token, self.now)
            self._ready.set()
        deadline = self.now() + device["expires_in"]
        interval = device["interval"]
        while self.now() < deadline:
            if not self._wait(interval, cancelled):
                return
            try:
                state, payload = _poll_device(self.definition, device, self.poster)
            except (OSError, ValueError):
                self._fail(attempt, self.definition["name"]
                           + " sign-in could not reach the provider.")
                return
            if state == "pending":
                continue
            if state == "slow_down":
                interval += 5
                continue
            if state != "approved":
                self._fail(attempt, self.definition["name"]
                           + " sign-in was declined or expired.")
                return
            self._finish_approved(attempt, device, payload)
            return
        self._fail(attempt, self.definition["name"] + " sign-in timed out.")

    def _finish_approved(self, attempt, device, payload):
        with self._lock:
            if attempt != self._attempt:
                return
        try:
            tokens = _approval_tokens(self.definition, payload, self.poster)
        except OAuthError as exc:
            self._fail(attempt, str(exc))
            return
        except (OSError, ValueError):
            self._fail(attempt, self.definition["name"]
                       + " sign-in could not be completed. Start again.")
            return
        endpoint = _token_endpoint_for(self.definition, device)
        with self.grant_store.refresh_guard():
            with self._lock:
                if attempt != self._attempt:
                    return
                if _read_status(self.status_path).get("attempt") != attempt:
                    return
                try:
                    _save_tokens(self.grant_store, self.provider, tokens, "", endpoint,
                                 self.now())
                except (OAuthError, KeychainError):
                    self._attempt = ""
                    _write_status(self.status_path, "error", "", "",
                                  "The app-owned Keychain could not save the sign-in.",
                                  "", self.process_token, self.now)
                    self._ready.set()
                    return
                self._attempt = ""
                _write_status(self.status_path, "signed_in", "", "", "Signed in.",
                              "", self.process_token, self.now)
                self._ready.set()


def resolve_grant(grant_store, *, provider=DEFAULT_PROVIDER, poster=None, now=time.time):
    """Return (access token, grant) for a provider, refreshing at most once."""
    definition = _store_definition(grant_store, provider)
    grant = grant_store.load()
    token = _usable_access_token(grant, now())
    if token:
        return token, grant
    with grant_store.refresh_guard():
        grant = grant_store.load()
        token = _usable_access_token(grant, now())
        if token:
            return token, grant
        if not isinstance(grant, dict):
            raise OAuthError(_sign_in_message(definition))
        refresh_token = str(grant.get("refresh_token") or "").strip()
        if not refresh_token:
            raise OAuthError(_sign_in_message(definition, "The saved sign-in cannot be refreshed."))
        endpoint = _trusted_token_endpoint(definition, grant)
        status, payload = _post_form(
            definition,
            endpoint,
            {"grant_type": "refresh_token", "client_id": definition["client_id"],
             "refresh_token": refresh_token},
            poster,
        )
        if status != 200:
            raise OAuthError(_sign_in_message(
                definition, "The saved sign-in expired or was rejected."))
        try:
            _save_tokens(grant_store, provider, payload, refresh_token, endpoint, now())
        except OAuthError:
            raise OAuthError(_sign_in_message(
                definition, "The provider did not return a usable grant.")) from None
        fresh = grant_store.load()
        if not isinstance(fresh, dict):
            raise OAuthError(_sign_in_message(definition))
        return str(fresh.get("access_token") or ""), fresh


def access_token(grant_store, *, provider=DEFAULT_PROVIDER, poster=None, now=time.time):
    """Return an access token or raise with an explicit sign-in instruction."""
    token, _grant = resolve_grant(grant_store, provider=provider, poster=poster, now=now)
    return token


def _account_headers(provider, grant):
    if provider != OPENAI_PROVIDER or not isinstance(grant, dict):
        return {}
    headers = {}
    account_id = str(grant.get("account_id") or "").strip()
    residency = str(grant.get("residency") or "").strip()
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    if residency:
        headers["x-openai-internal-codex-residency"] = residency
    return headers


class AccountRegistry:
    """One account service for every built-in provider in the manifest."""

    def __init__(self, home, *, manifest=None, manifest_path=None, keychain=None,
                 poster=None, getter=None, sleep=time.sleep, now=time.time, lock_root=None):
        self.home = Path(home)
        if manifest is None:
            manifest = load_manifest(manifest_path or DEFAULT_MANIFEST_PATH)
        self.manifest = manifest
        self.definitions = provider_definitions(manifest)
        self.now = now
        self.poster = poster
        self.process_token = uuid.uuid4().hex
        self.stores = {}
        self._logins = {}
        for provider, definition in self.definitions.items():
            lock_path = grant_lock_path(definition["keychain_service"])
            if lock_root is not None:
                lock_path = Path(lock_root) / lock_path.name
            store = GrantStore(definition["keychain_service"], keychain=keychain,
                               provider=provider, definition=definition, lock_path=lock_path)
            self.stores[provider] = store
            self._logins[provider] = LoginManager(
                self.home, store, provider, definition,
                poster=poster, getter=getter, sleep=sleep, now=now,
                process_token=self.process_token)

    def _login(self, provider):
        manager = self._logins.get(provider)
        if manager is None:
            raise OAuthError("Unknown account provider")
        return manager

    def definition(self, provider):
        self._login(provider)
        return dict(self.definitions[provider])

    def status(self, provider):
        return self._login(provider).status()

    def start(self, provider):
        return self._login(provider).start()

    def cancel(self, provider):
        return self._login(provider).cancel()

    def list(self):
        reports = []
        for provider in self.definitions:
            reports.append(self._login(provider).status())
        return reports

    def credential(self, provider):
        store = self.stores.get(provider)
        if store is None:
            raise OAuthError("Unknown account provider")
        token, grant = resolve_grant(store, provider=provider, poster=self.poster, now=self.now)
        headers = _account_headers(provider, grant)
        headers["User-Agent"] = self.definitions[provider]["user_agent"]
        return ResolvedCredential(token, "oauth", headers=headers)

    def cancel_all(self):
        for provider in self.definitions:
            try:
                self._login(provider).cancel()
            except (OAuthError, KeychainError):
                continue

"""Native-owned macOS Keychain reads and writes.

Model and voice secrets are fetched only when a request needs them. Each
account provider's OAuth grant is stored as one generic-password item, so no
token ever reaches settings.json, the chat log, or a session database. Nothing
here logs a value.

A model API key belongs to exactly one endpoint. The Keychain account for a
model key is the canonical endpoint text, so a key saved for provider A can
never be read after the configured base URL changes to provider B. The former
single "default" model item is migrated once to the endpoint configured at that
launch, read back, and then removed; a non-secret receipt pins the endpoint it
was migrated to so a later launch cannot rebind it somewhere else.
"""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import subprocess
import threading

from .locking import LOCK_EX, LOCK_UN, flock


LEGACY_MODEL_ACCOUNT = "default"
MODEL_MIGRATION_ACCOUNT = "legacy-endpoint-migration-v1"
MODEL_MIGRATION_VERSION = 1

_GRANT_LOCKS = {}
_GRANT_LOCKS_GUARD = threading.Lock()


def _shared_grant_lock(key):
    """One in-process lock per lock file, whichever store asks for it."""
    with _GRANT_LOCKS_GUARD:
        lock = _GRANT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _GRANT_LOCKS[key] = lock
        return lock


def canonical_model_endpoint(endpoint):
    """Return the Keychain account text for a model endpoint.

    Both the native settings form and this runtime must compute the same
    account. The rule is deliberately small: trim surrounding whitespace and
    remove trailing slashes after the host. Everything else stays exactly as
    typed, so the account can be read by a person when diagnosing a key.
    """
    if not isinstance(endpoint, str):
        return ""
    text = endpoint.strip()
    marker = text.find("://")
    if marker < 0:
        return ""
    host_start = marker + 3
    end = len(text)
    while end > host_start and text[end - 1] == "/":
        end -= 1
    if end <= host_start:
        return ""
    return text[:end]


class KeychainError(RuntimeError):
    pass


class KeychainAuthorizationRequired(KeychainError):
    pass


class Keychain:
    def __init__(self, runner=subprocess.run):
        self.runner = runner
        self._lock = threading.RLock()

    def native_available(self):
        executable = os.environ.get("GOBLIN_KEYCHAIN_HELPER", "")
        return bool(executable) and Path(executable).is_file()

    def _native(self, operation, service, account, value=None):
        executable = os.environ.get("GOBLIN_KEYCHAIN_HELPER", "")
        if not executable or not Path(executable).is_file():
            raise KeychainError("The bundled credential helper is unavailable")
        request = {"operation": operation, "service": service, "account": account}
        if value is not None:
            request["value"] = value
        timeout = 30
        if operation == "authorize":
            timeout = 120
        try:
            result = self.runner([executable], input=json.dumps(request),
                                 capture_output=True, text=True, timeout=timeout, check=False)
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise KeychainError("The app-owned credential request failed") from None
        if result.returncode != 0 or not isinstance(response, dict):
            raise KeychainError("The app-owned credential request failed")
        if response.get("state") == "error":
            message = str(response.get("message") or "Credential access failed")
            if response.get("code") == "authorization_required":
                raise KeychainAuthorizationRequired(message)
            raise KeychainError(message)
        return response

    def get(self, service, account="default"):
        if not service:
            raise KeychainError("Keychain service is not configured")
        with self._lock:
            response = self._native("get", service, account)
        state = response.get("state")
        if state == "missing":
            return "", "missing"
        if state != "available" or not isinstance(response.get("value"), str):
            raise KeychainError("Credential read did not complete")
        return response["value"], "available"

    def put(self, service, value, account="default"):
        if not service or not isinstance(value, str) or not value or "\x00" in value:
            raise KeychainError("Keychain value is empty or invalid")
        with self._lock:
            result = self._native("put", service, account, value)
        if result.get("state") != "saved":
            raise KeychainError("Keychain write failed")
        return True

    def delete(self, service, account="default"):
        with self._lock:
            result = self._native("delete", service, account)
        if result.get("state") != "deleted":
            raise KeychainError("Keychain delete failed")

    def authorize(self, service, account="default"):
        with self._lock:
            response = self._native("authorize", service, account)
        if response.get("state") not in ("authorized", "missing"):
            raise KeychainError("Keychain authorization did not complete")
        return response["state"]

    def exists(self, service, account="default"):
        """Report item presence without fetching the value or requesting OS UI."""
        if not service or not account or not self.native_available():
            return "unavailable"
        with self._lock:
            try:
                response = self._native("exists", service, account)
            except KeychainError:
                return "unavailable"
        state = response.get("state")
        if state in ("found", "missing"):
            return state
        return "unavailable"


class SecretProvider:
    """Scoped secret reads for this process only.

    The model key is read for one canonical endpoint account per request. When
    the configured endpoint changes, the old key is not visible to the new
    endpoint. A legacy single "default" model item is copied once to the
    endpoint configured at this launch, verified by readback, and then deleted.
    """

    def __init__(self, services, keychain=None, environ=None, model_endpoint=""):
        self.services = dict(services or {})
        self.keychain = keychain or Keychain()
        self.environ = environ if environ is not None else {}
        self.model_endpoint = canonical_model_endpoint(model_endpoint)
        # A short non-secret state string for diagnostics: migrated, no-legacy,
        # kept-existing, already-migrated, unavailable, and so on.
        self.model_migration = ""
        if self.model_endpoint:
            self.adopt_legacy_model_key(self.model_endpoint)

    def _read(self, label, service, account, required=True):
        try:
            value, state = self.keychain.get(service, account)
        except KeychainAuthorizationRequired:
            # A denied or cancelled authorization must reach the caller even
            # when the item is optional. Treating it as an absent key would
            # send an unauthenticated request instead of asking the operator.
            raise
        except KeychainError:
            if required:
                raise
            # An unavailable helper is not a missing secret. Optional reads
            # return empty so a keyless custom endpoint still works.
            return ""
        if state != "available":
            if required:
                raise KeychainError(label + " is not installed in Keychain")
            return ""
        return value

    def model_account(self, endpoint=None):
        if endpoint is None:
            return self.model_endpoint
        return canonical_model_endpoint(endpoint)

    def model_key(self, endpoint=None):
        service = self.services.get("model", "")
        if not service:
            return ""
        account = self.model_account(endpoint)
        if not account:
            return ""
        return self._read("model", service, account, required=False)

    def model_key_status(self, endpoint=None):
        """Key presence for one endpoint. Never reads the secret value."""
        service = self.services.get("model", "")
        account = self.model_account(endpoint)
        if not service or not account:
            return "unavailable"
        return self.keychain.exists(service, account)

    def delete_model_key(self, endpoint=None):
        service = self.services.get("model", "")
        account = self.model_account(endpoint)
        if not service or not account:
            raise KeychainError("Model endpoint is not configured")
        self.keychain.delete(service, account)

    def adopt_legacy_model_key(self, endpoint=None):
        """Copy a pre-endpoint model key once and retire the old item.

        Returns a short state string. Failure never raises and never includes
        a secret, so startup can continue with the model credential absent.
        """
        service = self.services.get("model", "")
        target = self.model_account(endpoint)
        if not service or not target:
            self.model_migration = "not-configured"
            return self.model_migration
        if len(target) > 256:
            self.model_migration = "endpoint-too-long"
            return self.model_migration
        if isinstance(self.keychain, Keychain) and not self.keychain.native_available():
            # A bare development process must not read or rewrite app-owned items.
            self.model_migration = "app-bridge-unavailable"
            return self.model_migration
        try:
            self.model_migration = self._adopt_legacy_model_key(service, target)
        except KeychainError:
            self.model_migration = "unavailable"
        return self.model_migration

    def _adopt_legacy_model_key(self, service, target):
        receipt_state, receipt = self._read_migration_receipt(service)
        if receipt_state == "invalid":
            return "receipt-invalid"
        if receipt_state == "unavailable":
            return "receipt-unavailable"
        if receipt is not None:
            return self._finish_recorded_migration(service, target, receipt)
        legacy = self.keychain.exists(service, LEGACY_MODEL_ACCOUNT)
        target_state = self.keychain.exists(service, target)
        if legacy == "unavailable" or target_state == "unavailable":
            return "legacy-unavailable"
        if legacy == "missing":
            # Record the endpoint this install first bound to, so an old item
            # that appears later can never be copied to a different endpoint.
            self._write_migration_receipt(service, target, "complete")
            return "no-legacy"
        if target_state == "found":
            self._write_migration_receipt(service, target, "complete")
            return "kept-existing"
        self._write_migration_receipt(service, target, "pending")
        return self._copy_legacy_model_key(service, target)

    def _finish_recorded_migration(self, service, target, receipt):
        if receipt.get("endpoint") != target:
            return "already-migrated"
        if receipt.get("state") == "complete":
            return "already-migrated"
        target_state = self.keychain.exists(service, target)
        legacy = self.keychain.exists(service, LEGACY_MODEL_ACCOUNT)
        if target_state == "unavailable" or legacy == "unavailable":
            return "legacy-unavailable"
        if target_state == "found":
            # A previous launch wrote the key but did not finish. Retire the
            # legacy item only when it holds the same secret.
            if legacy == "found" and self._same_secret(service, target):
                self.keychain.delete(service, LEGACY_MODEL_ACCOUNT)
            self._write_migration_receipt(service, target, "complete")
            return "already-migrated"
        if legacy == "missing":
            self._write_migration_receipt(service, target, "complete")
            return "already-migrated"
        return self._copy_legacy_model_key(service, target)

    def _same_secret(self, service, target):
        target_value, target_state = self.keychain.get(service, target)
        legacy_value, legacy_state = self.keychain.get(service, LEGACY_MODEL_ACCOUNT)
        if target_state != "available" or legacy_state != "available":
            return False
        return bool(target_value) and target_value == legacy_value

    def _copy_legacy_model_key(self, service, target):
        legacy_value, legacy_state = self.keychain.get(service, LEGACY_MODEL_ACCOUNT)
        if legacy_state != "available" or not legacy_value:
            return "legacy-unreadable"
        self.keychain.put(service, legacy_value, target)
        copied_value, copied_state = self.keychain.get(service, target)
        if copied_state != "available" or copied_value != legacy_value:
            # Do not leave an unverified copy or remove the only good item.
            try:
                self.keychain.delete(service, target)
            except KeychainError:
                pass
            return "copy-unverified"
        self.keychain.delete(service, LEGACY_MODEL_ACCOUNT)
        self._write_migration_receipt(service, target, "complete")
        return "migrated"

    def _read_migration_receipt(self, service):
        value, state = self.keychain.get(service, MODEL_MIGRATION_ACCOUNT)
        if state == "missing":
            return "missing", None
        if state != "available":
            return "unavailable", None
        try:
            payload = json.loads(value)
        except ValueError:
            return "invalid", None
        if not isinstance(payload, dict):
            return "invalid", None
        if payload.get("version") != MODEL_MIGRATION_VERSION:
            return "invalid", None
        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return "invalid", None
        if endpoint != canonical_model_endpoint(endpoint):
            return "invalid", None
        if payload.get("state") not in ("pending", "complete"):
            return "invalid", None
        return "available", payload

    def _write_migration_receipt(self, service, endpoint, state):
        payload = {
            "version": MODEL_MIGRATION_VERSION,
            "endpoint": endpoint,
            "state": state,
        }
        encoded = json.dumps(payload, sort_keys=True)
        self.keychain.put(service, encoded, MODEL_MIGRATION_ACCOUNT)

    def voice_key(self):
        service = self.services.get("voice", "")
        if not service:
            raise KeychainError("Voice Keychain service is not configured")
        return self._read("voice", service, "default")


def grant_lock_path(service):
    identity = (service + "\0default").encode("utf-8")
    filename = hashlib.sha256(identity).hexdigest() + ".lock"
    return Path.home() / "Library" / "Application Support" / "KeychainGrantLocks" / filename


class GrantStore:
    """App-owned sign-in grants in one Keychain item per provider.

    The store carries the manifest row it was built from and the lock file
    that serializes grant rotation, so every consumer holding this same
    instance (voice, model transport, account registry) refreshes through one
    lock. No value is ever logged or returned outside a save/load call.
    """

    def __init__(self, service, keychain=None, *, provider="xai", definition=None,
                 lock_path=None):
        if not isinstance(service, str) or not service.strip():
            raise KeychainError("Grant Keychain service is not configured")
        self.service = service.strip()
        self.keychain = keychain or Keychain()
        self.provider = str(provider or "")
        self.definition = definition if isinstance(definition, dict) else None
        self.lock_path = Path(lock_path) if lock_path else grant_lock_path(self.service)

    @contextmanager
    def refresh_guard(self):
        """Serialize refresh in this process and across processes, then release."""
        key = str(self.lock_path.resolve())
        with _shared_grant_lock(key):
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a+b")
            try:
                flock(handle, LOCK_EX)
                try:
                    yield
                finally:
                    flock(handle, LOCK_UN)
            finally:
                handle.close()

    def load(self):
        value, state = self.keychain.get(self.service, "default")
        if state != "available":
            return None
        try:
            payload = json.loads(value)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def save(self, tokens):
        if not isinstance(tokens, dict):
            raise KeychainError("Grant payload must be an object")
        encoded = json.dumps(tokens, sort_keys=True)
        self.keychain.put(self.service, encoded, "default")

    def delete(self):
        self.keychain.delete(self.service, "default")

    def report(self, now):
        try:
            payload = self.load()
        except KeychainAuthorizationRequired as error:
            return {"state": "authorization_required", "has_refresh": False,
                    "expires_at": 0, "status": str(error)}
        if payload is None:
            return {"state": "missing", "has_refresh": False, "expires_at": 0,
                    "status": "Not signed in"}
        access = str(payload.get("access_token") or "").strip()
        refresh = str(payload.get("refresh_token") or "").strip()
        expires_at = payload.get("expires_at") or 0
        if type(expires_at) is not int:
            expires_at = 0
        if not access and not refresh:
            return {"state": "missing", "has_refresh": False, "expires_at": 0,
                    "status": "Not signed in"}
        if not access or (expires_at > 0 and expires_at <= int(now)):
            return {"state": "expired", "has_refresh": bool(refresh), "expires_at": expires_at,
                    "status": "Access expired. Sign in again."}
        return {"state": "current", "has_refresh": bool(refresh), "expires_at": expires_at,
                "status": "Signed in"}

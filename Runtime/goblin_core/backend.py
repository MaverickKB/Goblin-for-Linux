"""Model transport for Goblin: HTTP Chat Completions, Responses, or a CLI worker.

Only the standard library is used. The model secret is fetched at request time
from the caller's scoped provider or account credential and is never stored on
this object.
"""

import http.client
import json
import math
import time
from urllib import error, request
from urllib.parse import urlsplit

from .http_cancel import CancellableHTTP, CancelledIO
from .providers import create_adapter
from .cli_worker import CliWorker, WorkerError

SIGN_IN_REQUIRED = "Sign in required. Connect the selected account in Settings."


def _credential_headers(credential):
    """Provider and account headers that travel beside the bearer token."""
    raw = getattr(credential, "headers", None) or {}
    if not isinstance(raw, dict):
        raise ValueError("Credential headers must be a name to value table")
    headers = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Credential header names must be text")
        if not isinstance(value, str) or not value:
            raise ValueError("Credential header values must be text")
        if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
            raise ValueError("Credential headers cannot contain line breaks")
        headers[name] = value
    return headers


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BackendError(RuntimeError):
    def __init__(self, message, category="provider", rate_limits=None, *, kind=None,
                 http_status=None, detail=""):
        super().__init__(message)
        self.category = category
        self.rate_limits = rate_limits or {}
        self.kind = category if kind is None else kind
        self.http_status = http_status
        self.detail = detail

    def diagnostics(self):
        categories = ("provider", "configuration", "authentication", "permission",
                      "rate_limit", "timeout", "connection", "cancelled")
        kinds = categories + ("http", "parse", "incomplete_response")
        category = self.category if self.category in categories else "provider"
        kind = self.kind if self.kind in kinds else "provider"
        result = {"error_category": category, "error_kind": kind}
        if kind == "http" and type(self.http_status) is int:
            result["http_status"] = self.http_status
        if isinstance(self.detail, str) and self.detail.strip():
            result["error_detail"] = self.detail[:1024]
        return result


class _StreamRedactor:
    def __init__(self, callback, secret):
        self.callback = callback
        self.secret = secret
        self.pending = {}

    def __call__(self, kind, text):
        text = self.pending.pop(kind, "") + text
        limit = max(0, len(text) - len(self.secret) + 1)
        start = 0
        parts = []
        while start < limit:
            match = text.find(self.secret, start)
            if match < 0:
                parts.append(text[start:limit])
                start = limit
                break
            parts.extend((text[start:match], "[REDACTED]"))
            start = match + len(self.secret)
        self.pending[kind] = text[start:]
        output = "".join(parts)
        if output:
            self.callback(kind, output)

    def flush(self):
        pending, self.pending = self.pending, {}
        for kind, text in pending.items():
            if text:
                self.callback(kind, text)


class Backend:
    """One provider binding.

    A caller supplies either key_provider (an endpoint-scoped API key) or
    credential_provider (a resolved account credential with value, source and
    headers). The credential provider is authoritative: when it fails, the
    request fails with a sign-in error and never falls back to an API key.
    """

    def __init__(self, base_url, model, api_kind, *, key_provider=None,
                 credential_provider=None, cli_command="", workspace="",
                 timeout=60.0, retries=2, stream=True, parameters=None, binding_key=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "")
        self.api_kind = api_kind
        self.key_provider = key_provider
        self.credential_provider = credential_provider
        self.binding_key = binding_key
        self.cli_command = str(cli_command or "")
        self.workspace = str(workspace or "")
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.stream = bool(stream)
        self.parameters = dict(parameters or {})
        self.worker = None
        self.adapter = None
        self.cancel_event = None
        self.on_delta = None
        self.last_auth = ""
        self._transport = CancellableHTTP(_NoRedirect())
        self._opener = self._transport.opener
        if api_kind == "cli_worker":
            vendor = self._vendor()
            options = {"vendor": vendor, "command": self.cli_command, "cwd": self.workspace,
                       "timeout": 300.0, "model": self.model, "engine_tools": True}
            self.worker = CliWorker(options)
        else:
            kind = {"chat_completions": "openai_chat", "responses": "openai_responses",
                    "codex_responses": "codex_responses"}[api_kind]
            self.adapter = create_adapter(kind)

    def _vendor(self):
        from pathlib import Path
        name = Path(self.cli_command).name.lower() if self.cli_command else ""
        for vendor in ("codex", "claude", "grok"):
            if name == vendor or name.startswith(vendor + "."):
                return vendor
        return ""

    def cancel(self):
        self._transport.cancel()
        if self.worker is not None:
            self.worker.cancel()
        return True

    def _resolve_credential(self):
        """Fetch this request's account credential. There is no key fallback."""
        provider = self.credential_provider
        if provider is None:
            raise BackendError(SIGN_IN_REQUIRED, "authentication")
        try:
            credential = provider()
        except Exception:
            raise BackendError(SIGN_IN_REQUIRED, "authentication") from None
        value = getattr(credential, "value", "")
        if not isinstance(value, str) or not value.strip():
            raise BackendError(SIGN_IN_REQUIRED, "authentication")
        return credential

    def _api_key(self):
        if self.key_provider is None:
            return ""
        try:
            return self.key_provider()
        except Exception as exc:
            raise BackendError("Model credential unavailable (" + type(exc).__name__ + ")",
                               "configuration") from None

    def check_cancelled(self):
        self._transport.cancel_event = self.cancel_event
        try:
            self._transport.check_cancelled()
        except CancelledIO:
            raise BackendError("Model request cancelled by operator", "connection") from None

    def chat(self, messages, tools=None, **kw):
        self._transport.reset()
        self.check_cancelled()
        if self.worker is not None:
            return self._chat_cli(messages, tools, **kw)
        return self._chat_http(messages, tools, **kw)

    def _chat_cli(self, messages, tools, **kw):
        try:
            self.worker.on_delta = self.on_delta
            try:
                result = self.worker.chat(messages, None if kw.get("tool_choice") == "none" else tools, **kw)
            finally:
                self.worker.on_delta = None
            self.last_auth = "cli_login"
            return result
        except WorkerError as exc:
            failure = getattr(self.worker, "last_failure", None) or {}
            tail = failure.get("stderr_tail") or failure.get("stdout_tail") or ""
            raise BackendError(str(exc), exc.category, detail=tail) from None

    def _chat_http(self, messages, tools, **kw):
        try:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username is not None or not self.model:
                raise ValueError("Provider endpoint and model must be configured")
            if not math.isfinite(self.timeout) or self.timeout <= 0 or self.retries < 0:
                raise ValueError("Invalid transport limits")
            options = dict(self.parameters)
            options.update(kw)
            thinking_budget = options.get("thinking_budget", 0)
            if type(thinking_budget) is int and thinking_budget > 0:
                for name in ("max_tokens", "max_completion_tokens"):
                    allowance = options.get(name)
                    if type(allowance) is int and allowance > 0:
                        options[name] = allowance + thinking_budget
            headers = {"Content-Type": "application/json"}
            payload = self.adapter.encode(self.model, messages, tools, options)
            assembles = hasattr(self.adapter, "assemble")
            streaming = assembles and (self.stream or getattr(self.adapter, "requires_streaming", False))
            if streaming:
                payload["stream"] = True
                headers["Accept"] = "text/event-stream"
            else:
                headers["Accept"] = "application/json"
            credential_headers = {}
            if self.credential_provider is not None:
                credential = self._resolve_credential()
                key = credential.value.strip()
                credential_headers = _credential_headers(credential)
                auth_source = credential.source or "account"
            else:
                key = self._api_key()
                auth_source = "api_key" if key else "none"
            headers.update(credential_headers)
            # The adapter owns the auth scheme for the token; account headers
            # only add provider identity beside it.
            headers.update(self.adapter.headers(key, options))
            self.last_auth = auth_source
            encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
            path = self.adapter.path
            url = self.base_url + path
        except (ValueError, TypeError, KeyError):
            raise BackendError("Invalid provider request configuration; check endpoint, model and messages", "configuration") from None
        for attempt in range(self.retries + 1):
            self.check_cancelled()
            req = request.Request(url, data=encoded, headers=headers, method="POST")
            output_started = False

            def emit(kind, text):
                nonlocal output_started
                if text and kind in ("text", "thought", "tool"):
                    output_started = True
                if self.on_delta:
                    self.on_delta(kind, text)

            response_phase = "headers"
            content_type = ""
            try:
                if streaming:
                    emit("status", "Requesting model response" if attempt == 0 else "Retrying model connection")
                with self._opener.open(req, timeout=self.timeout) as response:
                    self._transport.response = response
                    self.check_cancelled()
                    content_type = (getattr(response, "headers", {}) or {}).get("Content-Type", "")
                    if streaming and (getattr(self.adapter, "requires_streaming", False)
                                      or "text/event-stream" in content_type):
                        response_phase = "event stream"
                        emit("status", "Connected, waiting for model output")
                        redactor = _StreamRedactor(emit, key) if key else None
                        if redactor is None:
                            data = self.adapter.assemble(response, emit)
                        else:
                            try:
                                data = self.adapter.assemble(response, redactor)
                            finally:
                                redactor.flush()
                    else:
                        response_phase = "JSON body"
                        data = json.load(response)
                self.check_cancelled()
                result = self.adapter.decode(_redact_secret(data, key))
                return result
            except error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                detail = ""
                if status not in (401, 403):
                    try:
                        detail = exc.read(400).decode("utf-8", "replace").strip()
                        if key:
                            detail = detail.replace(key, "[REDACTED]")
                    except (OSError, ValueError):
                        detail = ""
                exc.close()
                if status != 429 and not 500 <= status <= 599:
                    category = {401: "authentication", 403: "permission"}.get(status, "provider")
                    message = "Backend: HTTP " + str(status)
                    if status == 401 and self.credential_provider is not None:
                        # A rejected account grant needs the account repair path.
                        message = SIGN_IN_REQUIRED
                    raise BackendError(message, category, kind="http",
                                       http_status=status, detail=detail) from None
                if output_started or attempt == self.retries:
                    category = "rate_limit" if status == 429 else "provider"
                    raise BackendError("Backend: HTTP " + str(status), category,
                                       kind="http", http_status=status) from None
                pause = 0.5 * (2 ** attempt)
                try:
                    pause = max(pause, float(retry_after))
                except ValueError:
                    pass
                if self._transport._cancelled.wait(min(pause, 30)):
                    self.check_cancelled()
            except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                self.check_cancelled()
                if output_started or attempt == self.retries:
                    cause = exc.reason if isinstance(exc, error.URLError) else exc
                    category = "timeout" if isinstance(cause, TimeoutError) else "connection"
                    raise BackendError("Backend: transport failed (" + type(exc).__name__ + ")", category) from None
                if self._transport._cancelled.wait(min(0.5 * (2 ** attempt), 30)):
                    self.check_cancelled()
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                self.check_cancelled()
                detail = type(exc).__name__ + ": " + str(exc)
                if isinstance(exc, json.JSONDecodeError):
                    detail += "; phase=" + response_phase
                    detail += "; content_type=" + content_type[:80]
                    detail += "; response_characters=" + str(len(exc.doc))
                    if exc.doc.startswith(("data:", "event:")):
                        detail += "; framing=event_stream"
                if key:
                    detail = detail.replace(key, "[REDACTED]")
                raise BackendError("Backend: invalid provider response", kind="parse", detail=detail[:400]) from None
        raise BackendError("Backend exhausted retries")


def _redact_secret(value, secret):
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned[_redact_secret(key, secret)] = _redact_secret(item, secret)
        return cleaned
    if isinstance(value, list):
        cleaned = []
        for item in value:
            cleaned.append(_redact_secret(item, secret))
        return cleaned
    return value

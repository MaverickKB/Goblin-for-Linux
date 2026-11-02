"""xAI duplex voice leases over one Goblin session.

The provider socket is opened by the native app. This module mints the ephemeral
client secret, builds the real session.update with the same tools and history,
and brokers transcript/tool events into the one session's ChatLog.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from . import account_oauth
from .log import ChatLog
from .prompts import SYSTEM_PROMPT_TEMPLATE
from .tree import SummaryTree
from .view import View

TOKEN = r"[A-Za-z0-9!#$%&'*+\-.^_\x60|~]+"
TRANSCRIPTS = {"conversation.item.input_audio_transcription.completed": "user",
               "response.output_audio_transcript.done": "agent"}
STOP_WORDS = {"stop", "cancel", "quiet"}
TRANSCRIBE_MODEL = "grok-transcribe"

SESSION_TASK = {
    "type": "function", "name": "session_task",
    "description": "Dispatch work to this same session with its full tools, permissions and history, without blocking voice; or read its task status.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["submit", "status"]},
        "text": {"type": "string", "description": "Complete task and relevant spoken context for the session."},
        "turn_id": {"type": "string"}}, "required": ["action"], "additionalProperties": False}}
VOICE_CONVERSATION_STYLE = """
Speak in one or two concise sentences by default. Acknowledge a tool request
before the lookup, then give its useful result briefly. Offer details rather
than reading long reports aloud.
"""
HANDOFF_INSTRUCTIONS = """
You are the spoken side of this selected session, sharing its identity and conversation.
For work requiring tools, use session_task submit with the user's request and relevant
conversation context. The selected session performs it with its full tools and permissions.
Acknowledge accepted work briefly and keep talking naturally while it runs. Do not claim
it is complete from an acceptance receipt. Completed task status arrives automatically;
explain the actual result naturally once, without starting the task again. Use status when
asked about progress. Do not submit ordinary conversation as a second task.
"""


def _words(text):
    return re.findall(r"[\w']+", text.lower())


class VoiceError(ValueError):
    def __init__(self, category, message=""):
        self.category = category
        if message:
            super().__init__(message)
        else:
            super().__init__("Voice operation unavailable (" + category + ")")


def configured(settings):
    return bool(settings.websocket_url and settings.model and settings.api_base_url
                and settings.client_secret_path and settings.client_protocol_prefix
                and getattr(settings, "auth", "oauth") in ("oauth", "api_key"))


def pipes(settings):
    return {"duplex": configured(settings), "local": False}


def selected_pipe(settings):
    return "duplex" if configured(settings) else ""


def validate_voice(settings):
    if settings.websocket_url:
        parsed = urlsplit(settings.websocket_url)
        if (parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username is not None
                or parsed.fragment or any(char.isspace() for char in settings.websocket_url)):
            raise ValueError("Voice WebSocket must be a credential-free WS(S) endpoint")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Voice WebSocket port is invalid") from None
        for key, _ in parse_qsl(parsed.query):
            if key.lower() in ("token", "api_key", "key", "authorization", "client_secret", "access_token"):
                raise ValueError("Voice WebSocket must not carry credentials")
    if not isinstance(settings.audio_format, str) or not settings.audio_format:
        raise ValueError("Voice audio format must be configured text")
    if settings.api_base_url:
        parsed = urlsplit(settings.api_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
            raise ValueError("Voice API base must be a credential-free HTTP(S) URL")
    if settings.client_secret_path:
        if settings.client_secret_path.startswith("//"):
            raise ValueError("Voice mint path must stay under the configured API base")
    for name in ("max_session_seconds", "secret_ttl_seconds", "max_response_bytes", "max_event_bytes",
                 "max_text_bytes", "max_events", "max_sessions", "audio_sample_rate"):
        value = getattr(settings, name)
        if type(value) is not int or value <= 0:
            raise ValueError("Voice durations and bounds must be positive integers")
    if settings.timeout <= 0 or settings.clock_skew_seconds < 0:
        raise ValueError("Invalid voice timeout or clock skew")
    if settings.tools_mode not in ("configured", "session", "handoff"):
        raise ValueError("Voice tools_mode must be configured, session or handoff")
    if not isinstance(settings.tools, list):
        raise ValueError("Voice tools must be an explicit list of names")
    if len(set(settings.tools)) != len(settings.tools):
        raise ValueError("Voice tool names must be unique")
    if not isinstance(settings.turn_detection, dict):
        raise ValueError("Voice turn detection must be a settings table")
    if not isinstance(settings.session_options, dict) or set(settings.session_options) & {"instructions", "tools", "model"}:
        raise ValueError("Voice context and tool schemas are server-owned")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class VoiceClient:
    def __init__(self, settings, *, secret_provider=None, grant_store=None, opener=None, clock=time.time):
        self.settings = settings
        self.secret_provider = secret_provider
        self.grant_store = grant_store
        self.opener = opener
        self.clock = clock
        self._secret = ""

    def _bearer(self):
        auth = getattr(self.settings, "auth", "oauth")
        if auth == "api_key":
            if self.secret_provider is None:
                raise VoiceError("credential")
            try:
                return self.secret_provider.voice_key()
            except Exception:
                raise VoiceError("credential") from None
        if self.grant_store is None:
            raise VoiceError("sign_in_required", "Sign in required. Open Settings and sign in with xAI.")
        try:
            return account_oauth.access_token(self.grant_store, now=self.clock)
        except account_oauth.OAuthError as error:
            raise VoiceError("sign_in_required", str(error)) from None

    def mint(self):
        settings = self.settings
        if not settings.enabled:
            raise VoiceError("disabled")
        if not configured(settings):
            raise VoiceError("configuration")
        try:
            self._secret = self._bearer()
            if not self._secret:
                raise VoiceError("credential")
            request = urllib.request.Request(
                settings.api_base_url.rstrip("/") + settings.client_secret_path,
                data=json.dumps({"expires_after": {"anchor": "created_at",
                                                   "seconds": settings.secret_ttl_seconds}}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._secret},
                method="POST")
            send = self.opener or urllib.request.build_opener(_NoRedirect()).open
            with send(request, timeout=settings.timeout) as response:
                raw = response.read(settings.max_response_bytes + 1)
            if len(raw) > settings.max_response_bytes:
                raise VoiceError("invalid_response")
            data = json.loads(raw)
            token = data.get("value")
            expires = data.get("expires_at")
            now = self.clock()
            if (not isinstance(token, str) or not re.fullmatch(TOKEN, token) or self._secret in token
                    or type(expires) not in (int, float) or not math.isfinite(expires)
                    or not now < expires <= now + settings.secret_ttl_seconds + settings.clock_skew_seconds):
                raise VoiceError("invalid_response")
            return {"value": token, "expires_at": expires}
        except VoiceError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            raise VoiceError("upstream") from None
        except TimeoutError:
            raise VoiceError("timeout") from None
        except urllib.error.URLError as error:
            raise VoiceError("timeout" if isinstance(error.reason, TimeoutError) else "upstream") from None
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise VoiceError("invalid_response") from None
        except Exception:
            raise VoiceError("upstream") from None


class GoblinVoiceSessions:
    def __init__(self, host, *, client=None, clock=time.time):
        self.host = host
        self.clock = clock
        self.settings = host.config.voice
        self.client = client
        self._leases = {}
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()

    def refresh_settings(self):
        self.settings = self.host.config.voice
        if self.client is not None:
            self.client.settings = self.settings

    def _fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self.settings), sort_keys=True, default=str).encode()).hexdigest()

    def status(self):
        enabled = self.settings.enabled
        with self._lock:
            leases = list(self._leases.values())
        count = 0
        if enabled and leases:
            now = self.clock()
            for lease in leases:
                if (not lease["stopped"] and now < lease["deadline"]
                        and lease["fingerprint"] == self._fingerprint()):
                    count += 1
        options = self.settings.session_options if isinstance(self.settings.session_options, dict) else {}
        return {"enabled": enabled, "configured": configured(self.settings), "active_count": count,
                "voices": [str(value) for value in self.settings.voices],
                "voice": str(options.get("voice", "")), "pipe": selected_pipe(self.settings),
                "pipes": pipes(self.settings), "auth": getattr(self.settings, "auth", "oauth")}

    def _require(self, slug):
        if not self.settings.enabled:
            raise VoiceError("disabled")
        if not configured(self.settings):
            raise VoiceError("configuration")
        session = self.host.session_for(slug)
        if session is None or getattr(session, "_closed", False):
            raise VoiceError("session")

    def _secrets(self):
        return []

    @staticmethod
    def _scrub(value, secrets):
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {GoblinVoiceSessions._scrub(key, secrets): GoblinVoiceSessions._scrub(item, secrets)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [GoblinVoiceSessions._scrub(item, secrets) for item in value]
        return value

    @contextmanager
    def _surface(self, slug, secrets):
        session = self.host.session_for(slug)
        if session is not None:
            session.log.add_secrets(secrets)
            yield session.log, session.view
            return
        raise VoiceError("session")

    def start(self, slug, voice=None, keepalive=False):
        self.refresh_settings()
        if voice is not None and (not isinstance(voice, str) or voice not in self.settings.voices):
            raise ValueError("Voice is not in voice.voices")
        if not isinstance(keepalive, bool):
            raise ValueError("keepalive must be a boolean")
        self._require(slug)
        with self._start_lock:
            self._require(slug)
            fingerprint = self._fingerprint()
            now = self.clock()
            with self._lock:
                self._leases = {key: lease for key, lease in self._leases.items()
                                if not lease["stopped"] and now < lease["deadline"]}
                if len(self._leases) >= self.settings.max_sessions:
                    raise VoiceError("limit")
            session = self.host.session_for(slug)
            context = session.view.ready_snapshot()
            if context is None:
                raise VoiceError("context_pending", "Conversation is compacting. Finish compaction before starting voice.")
            tools = []
            registry = session.tools
            if self.settings.tools_mode == "configured":
                registry = registry.restricted(self.settings.tools)
            if self.settings.tools_mode != "handoff":
                for schema in registry.schemas():
                    tools.append({"type": "function", **schema["function"]})
            if self.settings.tools_mode == "handoff":
                tools.append(SESSION_TASK)
            with self._surface(slug, self._secrets()) as (log, view):
                parts = [session.system_prompt]
                try:
                    parts.append(session.capture.inheritance_block())
                except Exception:
                    pass
                parts.append(context["gist"])
                instructions = "\n".join(parts) + VOICE_CONVERSATION_STYLE
                if self.settings.tools_mode == "handoff":
                    instructions += HANDOFF_INSTRUCTIONS
            minted = self.client.mint()
            self._require(slug)
            if self._fingerprint() != fingerprint:
                raise VoiceError("configuration")
            secrets = [self.client._secret, minted["value"], *self._secrets()]
            voice_id = uuid.uuid4().hex
            now = self.clock()
            deadline = now + self.settings.max_session_seconds
            lease = {"slug": slug, "deadline": deadline, "stopped": False, "secrets": secrets,
                     "fingerprint": self._fingerprint(), "tools": {tool["name"] for tool in tools},
                     "goal": "", "utterance": None, "utterances": {}, "stop_generation": None,
                     "tasks": {}, "calls": {}, "responses": {}, "pending": {}, "events": 0,
                     "lock": threading.RLock(), "keepalive": keepalive, "last_event": now}
            session.log.add_secrets(secrets)
            with self._lock:
                self._leases[voice_id] = lease
            parsed = urlsplit(self.settings.websocket_url)
            query = dict(parse_qsl(parsed.query))
            query[self.settings.model_query_param] = self.settings.model
            endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
            session_payload = deepcopy(self.settings.session_options)
            audio = session_payload.setdefault("audio", {})
            input_audio = audio.setdefault("input", {})
            output_audio = audio.setdefault("output", {})
            for direction in ("input", "output"):
                audio[direction].setdefault("format", {"type": self.settings.audio_format,
                                                       "rate": self.settings.audio_sample_rate})
            input_audio.setdefault("transcription", {"model": self.settings.transcribe_model or TRANSCRIBE_MODEL})
            detection = session_payload.pop("turn_detection", None)
            if not isinstance(detection, dict):
                detection = deepcopy(self.settings.turn_detection)
            detection = dict(detection)
            detection["type"] = "server_vad"
            detection.pop("create_response", None)
            if self.settings.session_shape == "openai":
                session_payload.setdefault("type", "realtime")
                configured_voice = session_payload.pop("voice", None)
                if voice is not None:
                    output_audio["voice"] = voice
                elif isinstance(configured_voice, str) and configured_voice:
                    output_audio["voice"] = configured_voice
                input_audio["turn_detection"] = detection
            else:
                target_voice = voice if voice is not None else self.settings.voice
                if isinstance(target_voice, str) and target_voice:
                    session_payload["voice"] = target_voice
                session_payload["turn_detection"] = detection
            session_payload.update(instructions=instructions, tools=tools)
            session_payload = self._scrub(session_payload, secrets)
            if self._scrub(endpoint, secrets) != endpoint:
                with self._lock:
                    self._leases.pop(voice_id, None)
                raise VoiceError("configuration")
            return {"voice_id": voice_id, "id": voice_id, "websocket_url": endpoint,
                    "protocols": [*self.settings.client_protocol_extra,
                                  self.settings.client_protocol_prefix + minted["value"]],
                    "expires_at": minted["expires_at"], "session_expires_at": deadline,
                    "max_session_seconds": self.settings.max_session_seconds,
                    "audio_sample_rate": self.settings.audio_sample_rate,
                    "audio_format": self.settings.audio_format,
                    "session_update": {"type": "session.update", "session": session_payload},
                    "session": deepcopy(session_payload)}

    def _lease(self, slug, voice_id):
        self._require(slug)
        with self._lock:
            lease = self._leases.get(voice_id) if isinstance(voice_id, str) else None
        if lease is None or lease["slug"] != slug or lease["stopped"] or self.clock() >= lease["deadline"]:
            raise VoiceError("expired")
        if lease["fingerprint"] != self._fingerprint():
            raise VoiceError("configuration")
        return lease

    def _key(self, voice_id, *parts):
        return hashlib.sha256(json.dumps([voice_id, *parts], sort_keys=True).encode()).hexdigest()

    def _validate_events(self, events, lease):
        if not isinstance(events, list) or len(events) + lease["events"] > self.settings.max_events:
            raise VoiceError("limit")
        for event in events:
            try:
                if not isinstance(event, dict) or len(json.dumps(event, allow_nan=False).encode()) > self.settings.max_event_bytes:
                    raise ValueError()
                kind = event.get("type")
                if not isinstance(kind, str):
                    raise ValueError()
                if kind == "input_audio_buffer.committed":
                    if not isinstance(event.get("item_id"), str) or not event["item_id"]:
                        raise ValueError()
                elif kind in TRANSCRIPTS:
                    if not isinstance(event.get("item_id"), str) or not event["item_id"]:
                        raise ValueError()
                    if not isinstance(event.get("transcript"), str) or len(event["transcript"].encode()) > self.settings.max_text_bytes:
                        raise ValueError()
                    if type(event.get("content_index", 0)) is not int or event.get("content_index", 0) < 0:
                        raise ValueError()
                elif kind == "response.function_call_arguments.done":
                    if event.get("name") not in lease["tools"]:
                        raise VoiceError("permission")
                    for field in ("call_id", "response_id"):
                        if not isinstance(event.get(field), str) or not event[field]:
                            raise ValueError()
                    arguments = json.loads(event["arguments"]) if isinstance(event.get("arguments"), str) else event.get("arguments")
                    if not isinstance(arguments, dict):
                        raise ValueError()
                    json.dumps(arguments, allow_nan=False)
                elif kind == "response.done":
                    if not isinstance(event.get("response"), dict) or not isinstance(event["response"].get("id"), str):
                        raise ValueError()
            except VoiceError:
                raise
            except (ValueError, TypeError, KeyError, RecursionError):
                raise VoiceError("event") from None

    def events(self, slug, voice_id, events):
        lease = self._lease(slug, voice_id)
        with lease["lock"]:
            self._lease(slug, voice_id)
            lease["last_event"] = self.clock()
            self._validate_events(events, lease)
            session = self.host.session_for(slug)
            messages = []
            send = []
            response_ids = []
            session_turn = None
            for event in events:
                self._lease(slug, voice_id)
                kind = event["type"]
                if kind == "input_audio_buffer.committed":
                    self._begin_utterance(voice_id, lease, event["item_id"])
                elif kind in TRANSCRIPTS:
                    key = self._key(voice_id, TRANSCRIPTS[kind], event["item_id"], event.get("content_index", 0))
                    text = self._scrub(event["transcript"], lease["secrets"])
                    if not text.strip():
                        continue
                    with self._surface(slug, lease["secrets"]) as (log, view):
                        rows = list(log.iter())
                        previous_version = None
                        already_recorded = False
                        for row in rows:
                            if row.meta.get("voice_event_key") != key:
                                continue
                            previous_version = row
                            if row.text == text:
                                already_recorded = True
                        if already_recorded:
                            continue
                        meta = {"voice_id": voice_id, "voice_event_key": key, "voice_transcript": True,
                                "provenance": "client_reported"}
                        previous = next((row for row in reversed(rows) if row.meta.get("voice_id") == voice_id), None)
                        if previous_version is not None:
                            meta["supersedes"] = previous_version.i
                        elif TRANSCRIPTS[kind] == "user" and previous is not None and previous.kind == "user" \
                                and _words(text)[:len(_words(previous.text))] == _words(previous.text) and _words(previous.text):
                            meta["supersedes"] = previous.i
                        message = log.append(TRANSCRIPTS[kind], text, meta)
                        messages.append(asdict(message))
                    if TRANSCRIPTS[kind] == "user":
                        lease["goal"] = text
                        self._begin_utterance(voice_id, lease, event["item_id"])
                        session_turn = self._hand_spoken(slug, text)
                        if session_turn is not None:
                            lease["utterance"] = None
                elif kind == "response.function_call_arguments.done":
                    send.append(self._tool(voice_id, lease, event))
                    if event["response_id"] not in response_ids:
                        response_ids.append(event["response_id"])
                elif kind == "response.done":
                    response = event["response"]
                    response_id = response["id"]
                    expected = set()
                    output = response.get("output", [])
                    if isinstance(output, list):
                        for item in output:
                            if isinstance(item, dict) and item.get("type") == "function_call" and isinstance(item.get("call_id"), str):
                                expected.add(item["call_id"])
                    lease["pending"][response_id] = {"status": response.get("status"), "expected": expected}
                    if response_id not in response_ids:
                        response_ids.append(response_id)
            for response_id in response_ids:
                continuation = self._continuation(voice_id, lease, response_id)
                if continuation is not None:
                    send.append(continuation)
            if not events:
                send.extend(self._task_completions(voice_id, lease))
            poll_after_ms = None
            for task in lease["tasks"].values():
                if not task["delivered"]:
                    poll_after_ms = 1000
            lease["events"] += len(events)
            return {"accepted": len(events), "messages": messages,
                    "send": self._scrub(send, lease["secrets"]), "session_turn": session_turn,
                    "poll_after_ms": poll_after_ms}

    def _begin_utterance(self, voice_id, lease, item_id):
        if item_id in lease["utterances"]:
            return
        lease["stop_generation"] = self.host.stop_generation
        lease["utterance"] = self._key(voice_id, "user", item_id)
        lease["utterances"][item_id] = lease["utterance"]

    def _hand_spoken(self, slug, text):
        words = _words(text)
        spoken_stop = False
        if words and len(words) <= 3:
            for word in words:
                if word in STOP_WORDS:
                    spoken_stop = True
                    break
        if not spoken_stop:
            return None
        try:
            stopped = self.host.interrupt(slug)
        except Exception:
            return None
        return {"mode": "interrupt", "turn_id": stopped.get("turn_id") if isinstance(stopped, dict) else None}

    def _continuation(self, voice_id, lease, response_id):
        pending = lease["pending"].get(response_id)
        if pending is None or pending["status"] != "completed":
            return None
        completed = set()
        for key, call in lease["calls"].items():
            if call["response_id"] == response_id:
                completed.add(key)
        if not completed or not pending["expected"] <= completed:
            return None
        if response_id not in lease["responses"]:
            lease["responses"][response_id] = {"type": "response.create",
                                               "event_id": self._key(voice_id, response_id, "continue")}
        return deepcopy(lease["responses"][response_id])

    def _tool(self, voice_id, lease, event):
        key = event["call_id"]
        arguments = json.loads(event["arguments"]) if isinstance(event["arguments"], str) else event["arguments"]
        request = {"name": event["name"], "arguments": self._scrub(arguments, lease["secrets"])}
        previous = lease["calls"].get(key)
        if previous:
            if previous["request"] != request or previous["response_id"] != event["response_id"]:
                raise VoiceError("event")
            return deepcopy(previous["send"])
        item = {"type": "conversation.item.create", "event_id": self._key(voice_id, key, "result"),
                "item": {"type": "function_call_output", "call_id": key,
                         "output": json.dumps({"error": "Voice tool result unavailable; not retried"})}}
        if event["name"] == "session_task":
            lease["calls"][key] = {"request": request, "response_id": event["response_id"], "send": item}
            try:
                output = self._session_task(lease, arguments)
            except (ValueError, RuntimeError, KeyError) as exc:
                output = {"error": str(exc)}
            item["item"]["output"] = self._scrub(json.dumps(output), lease["secrets"])
            return deepcopy(item)
        session = self.host.session_for(lease["slug"])
        if session is None or not session._turn_lock.acquire(blocking=False):
            raise VoiceError("busy")
        previous_goal = getattr(session, "_action_goal", "")
        try:
            self._lease(lease["slug"], voice_id)
            if lease["goal"]:
                session._action_goal = lease["goal"]
            lease["calls"][key] = {"request": request, "response_id": event["response_id"], "send": item}
            try:
                output, record, _contexts = session._call_tool(
                    {"id": "voice-" + self._key(voice_id, key), **request})
                if not record["success"]:
                    output = json.dumps({"error": "Voice tool was denied or failed"})
                item["item"]["output"] = self._scrub(output, lease["secrets"])
            except Exception:
                pass
        finally:
            session._action_goal = previous_goal
            session._turn_lock.release()
        return deepcopy(item)

    def _session_task(self, lease, arguments):
        action = arguments.get("action")
        if set(arguments) - {"action", "text", "turn_id"}:
            raise ValueError("Unknown session_task argument")
        if action == "submit":
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip() or not lease["utterance"]:
                raise ValueError("A current spoken request and task text are required")
            turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, lease["utterance"] + text))
            turn_id = self.host.submit(lease["slug"], text, source="voice", client_request_id=turn_id,
                                       expected_stop_generation=lease["stop_generation"])
            if turn_id not in lease["tasks"]:
                lease["tasks"][turn_id] = {"delivered": False}
            return {"turn_id": turn_id, "state": self.host.turn(lease["slug"], turn_id)["state"],
                    "message": "Accepted by the selected session. Continue the conversation; completion will arrive."}
        if action == "status":
            turn_id = arguments.get("turn_id")
            if turn_id not in lease["tasks"]:
                raise ValueError("Task does not belong to this voice call")
            return self._task_result(lease, turn_id)
        raise ValueError("session_task action must be submit or status")

    def _task_result(self, lease, turn_id):
        turn = self.host.turn(lease["slug"], turn_id)
        result = {"turn_id": turn_id, "state": turn["state"], "error": turn.get("error")}
        session = self.host.session_for(lease["slug"])
        replies = []
        if session is not None:
            for row in session.log.iter():
                if row.kind == "agent" and row.meta.get("request_id") == turn_id:
                    replies.append(row.text)
        result["replies"] = replies
        return result

    def _task_completions(self, voice_id, lease):
        frames = []
        for turn_id, task in lease["tasks"].items():
            if task["delivered"]:
                continue
            result = self._task_result(lease, turn_id)
            if result["state"] not in ("done", "error", "cancelled"):
                continue
            call_id = "task-" + self._key(voice_id, turn_id)
            frames.append({"type": "conversation.item.create", "event_id": call_id + "-call",
                           "item": {"type": "function_call", "call_id": call_id, "name": "session_task",
                                    "arguments": json.dumps({"action": "status", "turn_id": turn_id})}})
            frames.append({"type": "conversation.item.create", "event_id": call_id + "-output",
                           "item": {"type": "function_call_output", "call_id": call_id,
                                    "output": json.dumps(result)}})
            task["delivered"] = True
        if frames:
            frames.append({"type": "response.create", "event_id": self._key(voice_id, frames, "completion")})
        return frames

    def stop(self, slug, voice_id):
        self.refresh_settings()
        with self._lock:
            lease = self._leases.get(voice_id) if isinstance(voice_id, str) else None
        if lease is None or lease["slug"] != slug:
            # A lease id from the bridge may arrive as "id"; accept the same map.
            raise VoiceError("expired")
        with lease["lock"]:
            lease["stopped"] = True
            lease["secrets"] = []
        return {"stopped": True}

    def heartbeat(self, slug, voice_id):
        lease = self._lease(slug, voice_id)
        if not lease.get("keepalive"):
            return {"keepalive": False, "reaped": False}
        with lease["lock"]:
            self._lease(slug, voice_id)
            lease["last_event"] = self.clock()
        return {"keepalive": True, "reaped": False}

    def close(self):
        with self._lock:
            leases = list(self._leases.values())
        for lease in leases:
            with lease["lock"]:
                lease["stopped"] = True
                lease["secrets"] = []

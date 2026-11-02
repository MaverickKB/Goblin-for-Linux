"""The single-Goblin host: one config, one session, one turn queue, one floor."""

from collections import OrderedDict
from contextlib import contextmanager
import fnmatch
import json
import base64
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from urllib import error, request

from . import attachments as attachment_store
from .aethermind import AetherMindClient
from .aethermind_direct import DirectMcpClient
from .account_oauth import AccountRegistry, OAuthError
from .autonomy import Autonomy
from .autonomy_runtime import AutonomyManagerAdapter
from .backend import Backend, BackendError, _NoRedirect
from .capture import Capture
from .config import Config, ConfigError, load_config, normalize_settings, workspace_issue, write_settings
from .durability import sync_directory
from .floor_definition import (FloorDefinitionError, export_text, import_text,
                               load_definition, save_definition, default_definition,
                               peek_world_view, staged_world_view, validate_definition, exercise_definition)
from .intake import IntakeJournal
from .history_store import HistoryStore
from .http_cancel import CancellableHTTP, CancelledIO
from .keychain import KeychainError, SecretProvider, canonical_model_endpoint
from .model_seat import ModelClientPool
from .session import GoblinSession
from .tools import approval_rule
from .voice import GoblinVoiceSessions, VoiceClient, VoiceError


CANCELLED_BY_STOP = "Cancelled by operator Stop"
CANCELLED_BY_SHUTDOWN = "Cancelled by shutdown"
CANCELLED_ON_RECOVERY = "Interrupted before it ran"


def presentation_id(value):
    if not isinstance(value, str):
        return ''
    if len(value.encode('utf-8')) <= 512:
        return value
    return 'saved-' + hashlib.sha256(value.encode('utf-8')).hexdigest()


class ApprovalRequest:
    def __init__(self, approval_id, name, arguments):
        self.id = approval_id
        self.name = name
        self.arguments = arguments
        self.event = threading.Event()
        self.accepted = False


class GoblinHost:
    def __init__(self, home, manifest_path, emit=None, *, application_home=None):
        self.home = Path(home).expanduser().resolve()
        self.application_home = Path(application_home or home).expanduser().resolve()
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        self._emit_callback = emit or (lambda event, data: None)
        self._admission_lock = threading.RLock()
        self._session_operations = 0
        self._maintenance = False
        self._maintenance_owner = None
        self._retired_histories = {}
        self._history_lock = threading.RLock()
        self._history_context = []
        self._gist_snapshot = None
        self._event_lock = threading.RLock()
        self._revision = 0
        self._settings_revision = 0
        self._settings_lock = threading.RLock()
        self._active_turn = ("", "")
        self._memory_lock = threading.Lock()
        self._memory_refreshing = False
        self._memory_state = "loading"
        self._config = load_config(self.home, self.manifest_path)
        # A bad saved workspace must not stop bootstrap. Keep the configured
        # path visible and let Settings repair it through a live host.
        self._workspace_issue = workspace_issue(self._config.workspace, create=True)
        self.slug = self._config.slug
        self.error = ""
        self.stop_generation = 0
        self.operator_stopped = False
        self._operator_stop_path = self.home / "operator-stop.json"
        self._approvals = OrderedDict()
        self._approval_lock = threading.RLock()
        self._turns = OrderedDict()
        self._turn_lock = threading.RLock()
        self._queue = []
        self._intake = IntakeJournal(self.home)
        self._settings_issue = ""
        self._worker = None
        self._stopping = False
        self._backend_cache = None
        self._backend_lock = threading.RLock()
        self._compactor_clients = ModelClientPool(self._build_backend)
        self._account_checks_lock = threading.RLock()
        self._account_checks = {}
        self._credential_authorization_lock = threading.Lock()
        self._layer_count = 0
        self._layer_checked = 0.0
        services = self._config.manifest.get("keychain_services") or {}
        self.secret_provider = SecretProvider(services, model_endpoint=self._config.model_base_url)
        self.accounts = AccountRegistry(self.home, manifest=self._config.manifest)
        self.grant_store = self.accounts.stores["xai"]
        self._history_anchor = self.home / "chat" / self.slug
        self.history_store = HistoryStore(self.home, self.slug)
        self.history_id = self.history_store.active
        root = self.history_store.root(self.history_id)
        root.mkdir(parents=True, exist_ok=True)
        self._active_memory_settings = self._config.aethermind
        self.memory, self.capture, self._bootstrap = self._make_memory_capture(self._config, self._history_anchor)
        self.session = GoblinSession(lambda: self._config, self._backend,
                                     root, capture=self.capture, approval=self._approve,
                                     delta_sink=self._stream, on_message=self._message,
                                     host=self, slug=self.slug,
                                     compactor_backend_factory=self._compaction_backend,
                                     compactor_client_lease=self._compactor_client_lease)
        self.session.compactor.on_progress = self._compaction_progress
        self.voice_client = VoiceClient(
            self._config.voice, secret_provider=self.secret_provider, grant_store=self.grant_store)
        self.voice = GoblinVoiceSessions(self, client=self.voice_client)
        self._manager = AutonomyManagerAdapter(self)
        self.autonomy = Autonomy(self._manager)
        self.session.attach_host_tools(self)
        self._load_operator_stop()
        self._recover_pending_intake()
        if self._intake.recovery_notice:
            self.error = self._intake.recovery_notice
        self._apply_autonomy_lifecycle(self._config, None)

    @contextmanager
    def session_operation(self):
        with self._admission_lock:
            if self._maintenance and self._maintenance_owner != threading.get_ident():
                raise RuntimeError('History or autonomy setup is changing; try again when it finishes')
            self._session_operations += 1
            session = self.session
        try:
            yield session
        finally:
            with self._admission_lock:
                self._session_operations -= 1

    @contextmanager
    def idle_maintenance(self):
        with self._admission_lock:
            if self._maintenance or self._session_operations or self.busy():
                raise RuntimeError('Finish current session work before changing history or autonomy setup')
            if self.session.agents.active() or self.pending_approvals() or self._intake.entries():
                raise RuntimeError('Finish workers, approvals and accepted input before changing history or autonomy setup')
            with self.voice._lock:
                voice_active = any(not lease['stopped'] for lease in self.voice._leases.values())
            if voice_active:
                raise RuntimeError('Finish voice sessions before changing history or autonomy setup')
            self._maintenance = True
            self._maintenance_owner = threading.get_ident()
            autonomy = self.autonomy
        prior_paused = None
        try:
            prior_paused = autonomy.pause_production()
            if autonomy.active_actions():
                raise RuntimeError('Finish autonomy jobs and approvals before changing history or autonomy setup')
            yield self.session
        finally:
            with self._admission_lock:
                try:
                    if prior_paused is not None:
                        self.autonomy.resume_production(prior_paused)
                finally:
                    self._maintenance = False
                    self._maintenance_owner = None

    def history_list(self):
        with self.session_operation():
            return {'history_id': self.history_id, 'histories': self.history_store.list()}

    def history_rotate(self):
        with self.idle_maintenance():
            identifier = self.history_store.create()
            self._activate_history(identifier)
            snapshot = self.snapshot()
        self._emit('snapshot', snapshot)
        return snapshot

    def history_select(self, identifier):
        with self.idle_maintenance():
            if identifier != self.history_id:
                self._activate_history(identifier)
            snapshot = self.snapshot()
        self._emit('snapshot', snapshot)
        return snapshot

    def _activate_history(self, identifier):
        root = self.history_store.root(identifier)
        retired = self._retired_histories.get(identifier)
        if retired is not None and not retired['released'].is_set():
            raise RuntimeError('Selected history is still finishing its background summaries')
        root.mkdir(parents=True, exist_ok=True)
        active_capture = self.capture
        client = active_capture.client
        capture = Capture(client, active_capture.outbox_dir, active_capture.project_id,
                          active_capture.personal_project, active_capture.author,
                          anchor=active_capture.anchor,
                          inheritance_budget=active_capture.inheritance_budget,
                          personal_budget=active_capture.personal_budget,
                          note_project=active_capture.note_project,
                          recall_limit=active_capture.recall_limit,
                          omit_outcomes=active_capture.omit_outcomes,
                          inheritance_refresh_seconds=active_capture.inheritance_refresh_seconds)
        capture.tag_as(active_capture.goblin_id, active_capture.holder, active_capture.thread)
        bootstrap = self._bootstrap
        replacement = GoblinSession(lambda: self._config, self._backend, root, capture=capture,
                                    approval=self._approve, host=self, slug=self.slug,
                                    compactor_backend_factory=self._compaction_backend,
                                    compactor_client_lease=self._compactor_client_lease)
        try:
            replacement.attach_host_tools(self)
            self.history_store.select(identifier)
        except Exception:
            # No input has been admitted to this prepared session.
            replacement.close()
            raise
        with self._history_lock:
            previous = self.session
            previous_id = self.history_id
            self.session = replacement
            self.capture = capture
            self.memory = client
            self._bootstrap = bootstrap
            self.history_id = identifier
            replacement.delta_sink = self._stream
            replacement._on_message = self._message
            replacement.compactor.on_progress = self._compaction_progress
            previous.delta_sink = None
            previous._on_message = None
            previous.compactor.on_progress = lambda state: self._emit('compaction', dict(state, history_id=previous_id))
            released = threading.Event()
            self._retired_histories[previous_id] = {"released": released}
            self._history_context = []
            self._gist_snapshot = None
            with self._turn_lock:
                self._turns = OrderedDict()
                self._active_turn = ('', '')
            with self._manager._lock:
                self._manager._runtimes.clear()
            with self._event_lock:
                self._revision += 1
        threading.Thread(target=self._retire_history, args=(previous, released),
                         name='goblin-retired-history', daemon=True).start()

    def _retire_history(self, session, released):
        session.compactor.quiesce()
        try:
            session.close()
        finally:
            released.set()

    @property
    def config(self):
        return self._config

    def session_runtime(self, session=None):
        return self._manager.runtime(self.slug, internal=True)

    def _load_operator_stop(self):
        path = self._operator_stop_path
        if not path.exists():
            self.operator_stopped = False
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.operator_stopped = False
            return
        self.operator_stopped = bool(isinstance(payload, dict) and payload.get("stopped"))

    def _save_operator_stop(self):
        path = self._operator_stop_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps({"stopped": bool(self.operator_stopped)}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)

    def stop_generation_snapshot(self):
        """The accepted Stop generation, read under the turn lock."""
        with self._turn_lock:
            return self.stop_generation

    def _user_row_for_turn(self, turn_id):
        """The durable user row for a request identity, or None."""
        for message in self.session.log.iter():
            if message.kind != "user":
                continue
            meta = message.meta or {}
            if meta.get("request_id") == turn_id:
                return message
        return None

    def _register_available_model_key(self):
        """Register an available endpoint credential for log redaction.

        The endpoint is passed exactly as configured so a key saved for one
        provider cannot be applied to another. A missing or unreadable item is
        not a failure: the turn can still be accepted and the backend reports
        its own credential error.
        """
        binding = self._config.model_binding()
        connection, endpoint, _, api_kind, _, _, _, _ = binding
        if connection != "custom" or api_kind == "cli_worker":
            return ""
        if not endpoint:
            return ""
        try:
            key = self.secret_provider.model_key(endpoint=endpoint)
        except Exception:
            return ""
        if key:
            self.session.log.add_secrets([key])
        return key

    def _recover_pending_intake(self):
        """Turn entries left by an abrupt exit into durable canceled input.

        Recovery is idempotent: an entry whose request identity already has a
        user row is dropped without writing a second row or replaying work.
        """
        entries = self._intake.entries()
        if not entries:
            return
        self._register_available_model_key()
        for entry in entries:
            turn_id = entry["id"]
            existing = self._user_row_for_turn(turn_id)
            if existing is None:
                meta = dict(entry.get("meta") or {})
                meta["request_id"] = turn_id
                meta["source"] = entry.get("source") or "chat"
                meta["cancelled"] = True
                meta["cancel_reason"] = CANCELLED_ON_RECOVERY
                try:
                    self.session.log.append("user", entry.get("text") or "", meta)
                except Exception as error:
                    self.error = "Interrupted message could not be saved: " + type(error).__name__
                    continue
                item = {"id": turn_id, "text": entry.get("text") or "",
                        "source": entry.get("source") or "chat", "state": "cancelled",
                        "created": entry.get("created") or time.time(), "reply": "",
                        "error": CANCELLED_ON_RECOVERY, "meta": meta}
                with self._turn_lock:
                    self._remember_turn(item)
            self._intake.remove(turn_id)

    def clear_operator_stop(self):
        """Human continuation or Autonomy restart clears the durable Stop fence."""
        self.operator_stopped = False
        self._save_operator_stop()
        meta = self._manager._metadata.get(self.slug) or {}
        meta["stopped"] = False
        self._manager._metadata[self.slug] = meta

    def _emit(self, event, data):
        with self._event_lock:
            if event in ("message", "stream", "activity", "compaction"):
                data = dict(data)
                data.setdefault("history_id", self.history_id)
            if event in ("stream", "activity"):
                data = dict(data)
                data.setdefault("active_turn_id", self._active_turn[0])
                for key in ("turn_id", "active_turn_id", "response_id"):
                    if key in data:
                        data[key] = presentation_id(data[key])
            telemetry = event in ("stream", "compaction") or (event == "activity" and data.get("phase") in ("status", "heartbeat"))
            if event != "snapshot":
                if not telemetry:
                    self._revision += 1
                data = dict(data, revision=self._revision)
            try:
                self._emit_callback(event, data)
            except Exception:
                pass

    def _message(self, message):
        turn_id, source = self._active_turn
        meta = message.meta or {}
        turn_id = meta.get("request_id") or turn_id or "history-" + str(message.i)
        source = meta.get("source") or source
        for row in self._history_rows(message, turn_id, source):
            self._emit("message", row)

    def _compaction_progress(self, state):
        self._emit("compaction", state)

    def _stream(self, kind, text):
        if kind in ("thought", "text"):
            self._emit("stream", {"kind": "thought" if kind == "thought" else "agent", "text": text})
        elif kind == "model_start":
            # Streaming identity travels beside the display text, never inside it.
            self._emit("activity", {"phase": kind, "text": "", "response_id": text})
        elif kind == "compaction":
            state = json.loads(text)
            detail = "%s bytes, %s pending summaries" % (state['bytes'], state['pending'])
            self._emit("activity", {"phase": "compacting", "text": detail, "compaction": state})
        elif kind in ("status", "heartbeat", "tool"):
            self._emit("activity", {"phase": kind, "text": text})

    def _backend_binding(self, *, compaction=False):
        return self._config.model_binding(compaction=compaction)

    @contextmanager
    def _compactor_client_lease(self, backend, seat, stopped):
        binding = backend.binding_key
        with self._compactor_clients.use(binding, self._config.compactor.workers, stopped) as client:
            yield client, None

    def _backend(self, *, compaction=False):
        binding = self._backend_binding(compaction=compaction)
        with self._backend_lock:
            if self._backend_cache is None:
                self._backend_cache = {}
            if binding not in self._backend_cache:
                self._backend_cache[binding] = self._build_backend(binding)
            return self._backend_cache[binding]

    def _build_backend(self, binding):
        connection, endpoint, model, api_kind, cli_command, workspace, reasoning, thinking_budget = binding
        if api_kind == "cli_worker":
            if not cli_command:
                raise BackendError("Model is not configured: set a CLI command in Settings", "configuration")
            backend = Backend("", model, "cli_worker", cli_command=cli_command,
                              workspace=workspace)
            backend.parameters = {}
        else:
            if not endpoint or not model:
                raise BackendError("Model is not configured: set a model endpoint and model in Settings", "configuration")
            if connection == "custom":
                backend = Backend(endpoint, model, api_kind,
                                  key_provider=lambda: self._model_key(endpoint),
                                  workspace=workspace, timeout=60.0)
            else:
                backend = Backend(endpoint, model, api_kind,
                                  credential_provider=lambda: self._account_credential(connection),
                                  workspace=workspace, timeout=60.0)
            if reasoning != "off":
                backend.parameters["reasoning_effort"] = reasoning
                if thinking_budget > 0:
                    backend.parameters["thinking_budget"] = thinking_budget
        backend.binding_key = binding
        return backend

    def _compaction_backend(self):
        return self._backend(compaction=True)

    def _model_key(self, endpoint):
        key = self.secret_provider.model_key(endpoint=endpoint)
        session = getattr(self, "session", None)
        if session is not None:
            session.log.add_secrets([key])
        return key

    def _account_credential(self, provider):
        credential = self.accounts.credential(provider)
        session = getattr(self, "session", None)
        if session is not None:
            secrets = [credential.value]
            for name, value in credential.headers.items():
                if name.lower() == "chatgpt-account-id":
                    secrets.append(value)
            session.log.add_secrets(secrets)
        return credential

    def _approve(self, name, arguments):
        with self._approval_lock:
            approval_id = "ap-" + uuid.uuid4().hex[:12]
            request = ApprovalRequest(approval_id, name, arguments)
            self._approvals[approval_id] = request
            while len(self._approvals) > 100:
                old_id, old = next(iter(self._approvals.items()))
                if old.event.is_set():
                    self._approvals.pop(old_id, None)
                else:
                    break
        self._emit("approval", {"id": approval_id, "name": name, "arguments": arguments})
        self._emit("snapshot", self.snapshot())
        while not request.event.wait(0.1):
            if self._stopping or self.session._interrupt.is_set():
                request.accepted = False
                request.event.set()
                break
        with self._approval_lock:
            self._approvals.pop(approval_id, None)
        return request.accepted

    def pending_approvals(self):
        with self._approval_lock:
            return [{"id": item.id, "name": item.name, "arguments": item.arguments}
                    for item in self._approvals.values() if not item.event.is_set()]

    def resolve_approval(self, approval_id, accepted):
        if not isinstance(approval_id, str) or not approval_id:
            raise ValueError("approval id is required")
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be true or false")
        with self._approval_lock:
            request = self._approvals.get(approval_id)
            if request is None:
                raise ValueError("Approval is not pending: " + approval_id)
            request.accepted = accepted
            request.event.set()
        self._emit("activity", {"phase": "approval_resolved", "id": approval_id, "accepted": accepted})
        self._emit("snapshot", self.snapshot())
        return {"id": approval_id, "accepted": accepted}

    def _ingest_attachments(self, paths):
        if paths is None:
            return []
        if not isinstance(paths, list):
            raise ValueError("attachments must be an array of absolute paths")
        if len(paths) > 6:
            raise ValueError("Too many attachments")
        refs = []
        for path in paths:
            if not isinstance(path, str) or not path:
                raise ValueError("attachment must be an absolute path string")
            target = Path(path).expanduser()
            if not target.is_absolute():
                raise ValueError("attachment must be an absolute path")
            refs.append(attachment_store.reference(self.session.root, target))
        return refs

    def _remember_turn(self, item):
        """Keep live turn rows bounded. The caller holds the turn lock."""
        self._turns[item["id"]] = item
        while len(self._turns) > 200:
            old_id, old = next(iter(self._turns.items()))
            if old["state"] in ("done", "error", "cancelled"):
                self._turns.pop(old_id, None)
            else:
                break

    def _persist_input(self, item, reason):
        """Write the authoritative user row, then retire its journal entry.

        An append failure leaves the journal entry in place so the next launch
        can recover it. An existing row means the input is already durable.
        """
        turn_id = item.get("id") or ""
        existing = self._user_row_for_turn(turn_id)
        if existing is None:
            meta = dict(item.get("meta") or {})
            meta["request_id"] = turn_id
            meta["source"] = item.get("source") or "chat"
            meta["cancelled"] = True
            meta["cancel_reason"] = reason
            try:
                self.session.log.append("user", item.get("text") or "", meta)
            except Exception as error:
                self.error = "Accepted message could not be saved: " + type(error).__name__
                self._emit("activity", {"phase": "intake_save_failed", "turn_id": turn_id})
                return False
        self._intake.remove(turn_id)
        return True

    def _finish_fenced_turn(self, item, reason):
        persisted = self._persist_input(item, reason)
        with self._turn_lock:
            item["state"] = "cancelled" if persisted else "error"
            if not persisted:
                item["error"] = self.error or reason
        self._emit("snapshot", self.snapshot())

    def _finish_unrun_item(self, item, state, message):
        # Keep the row durable before publishing a terminal turn state, so a
        # reader that sees the turn settle can already find its text.
        persisted = self._persist_input(item, message)
        if not persisted:
            state = "error"
            message = self.error or message
        with self._turn_lock:
            item["state"] = state
            item["error"] = message
        if state == "error":
            self.error = message

    def _finalize_intake_entry(self, item, final_state=None):
        """Guarantee a durable raw row before dropping the journal entry."""
        turn_id = item.get("id") or ""
        state = final_state or item.get("state")
        existing = self._user_row_for_turn(turn_id)
        if existing is None and state == "done":
            # A completed turn without a raw row still needs its input kept.
            meta = dict(item.get("meta") or {})
            meta["request_id"] = turn_id
            meta["source"] = item.get("source") or "chat"
            try:
                self.session.log.append("user", item.get("text") or "", meta)
                self._intake.remove(turn_id)
            except Exception as error:
                self.error = "Accepted message could not be saved: " + type(error).__name__
            return
        if existing is not None:
            self._intake.remove(turn_id)
            return
        reason = item.get("error") or CANCELLED_ON_RECOVERY
        self._persist_input(item, reason)

    def submit(self, slug, text, source="chat", client_request_id=None, expected_stop_generation=None, turn_meta=None, attachments=None, expected_history_id=None):
        with self.session_operation():
            return self._submit(slug, text, source, client_request_id, expected_stop_generation, turn_meta, attachments, expected_history_id)

    def _submit(self, slug, text, source="chat", client_request_id=None,
               expected_stop_generation=None, turn_meta=None, attachments=None, expected_history_id=None):
        if expected_history_id is not None:
            if not isinstance(expected_history_id, str):
                raise ValueError('expected_history_id must be text')
            with self._admission_lock:
                if expected_history_id != self.history_id:
                    raise RuntimeError('History changed since this input was prepared; review the current history and send again')
        if slug != self.slug:
            raise ValueError("Unknown session: " + str(slug))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message text cannot be empty")
        if source == "chat":
            setup_issue = self.setup_issue()
            if setup_issue:
                # Refuse the operator's direct message before anything durable
                # is written. Settings and safe tools stay reachable so the
                # operator can repair the setup.
                raise RuntimeError("Setup needed: " + setup_issue)
        if client_request_id is not None and (not isinstance(client_request_id, str) or not client_request_id):
            raise ValueError("request id must be a nonempty string")
        if expected_stop_generation is not None:
            if isinstance(expected_stop_generation, bool) or not isinstance(expected_stop_generation, int):
                raise ValueError("expected stop generation must be an integer")
        # Operator Stop is durable against autonomous wakes until a human
        # continues or Autonomy is restarted. Human chat/steer clears it after
        # acceptance.
        if self.operator_stopped and source in ("autonomy", "wake", "timer", "task", "floor"):
            raise RuntimeError("Turn was fenced by operator Stop")
        turn_id = client_request_id or uuid.uuid4().hex
        meta = dict(turn_meta or {})
        meta.update({"request_id": turn_id, "source": source})
        if attachments:
            meta["attachments"] = attachments
        # Register an available endpoint credential before any input text is
        # written to the journal or to the raw chat log.
        self._register_available_model_key()
        committed = self._user_row_for_turn(turn_id)
        if committed is not None:
            committed_text = self.session.log.redact(text)
            if committed.text != committed_text:
                raise ValueError("Turn identity belongs to another input")
            self._intake.remove(turn_id)
            with self._turn_lock:
                if turn_id not in self._turns:
                    state = "cancelled" if committed.meta.get("cancelled") else "done"
                    self._remember_turn({
                        "id": turn_id, "text": text, "source": source, "state": state,
                        "created": time.time(), "reply": "", "error": "",
                        "meta": meta,
                    })
            return turn_id
        journal_text = self.session.log.redact(text)
        self._intake.add({"id": turn_id, "text": journal_text, "source": source,
                          "created": time.time(), "meta": self.session.log.redact(meta)})
        fenced = False
        fence_reason = CANCELLED_BY_STOP
        stopped_source = False
        fenced_item = None
        with self._turn_lock:
            existing = self._turns.get(turn_id)
            if existing is not None:
                if existing["text"] != text:
                    raise ValueError("Turn identity belongs to another input")
                return turn_id
            if self._stopping:
                fenced = True
                fence_reason = CANCELLED_BY_SHUTDOWN
            elif self.operator_stopped and source in ("autonomy", "wake", "timer", "task", "floor"):
                stopped_source = True
            elif expected_stop_generation is not None and expected_stop_generation != self.stop_generation:
                fenced = True
            if fenced:
                # Reserve the identity in the same lock that made the fence
                # decision, so a concurrent resend cannot queue a second turn.
                fenced_item = {"id": turn_id, "text": text, "source": source,
                               "state": "running", "created": time.time(), "reply": "",
                               "error": fence_reason, "meta": meta}
                self._remember_turn(fenced_item)
            elif not stopped_source:
                # Capture the Stop generation at admission so a Stop accepted
                # during endpoint preflight cannot be erased by the worker.
                item = {"id": turn_id, "text": text, "source": source, "state": "queued",
                        "created": time.time(), "reply": "", "error": "", "meta": meta,
                        "admitted_generation": self.stop_generation}
                self._remember_turn(item)
                self._queue.append(item)
                self._emit("activity", {"phase": "queued", "turn_id": turn_id, "source": source})
                if source in ("chat", "steer", "voice") and self.operator_stopped:
                    # Do this under the turn lock so a concurrent Stop cannot
                    # be cleared by a frame that Stop already fenced.
                    self.clear_operator_stop()
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._work, name="goblin-turns", daemon=True)
                    self._worker.start()
        if stopped_source:
            self._intake.remove(turn_id)
            raise RuntimeError("Turn was fenced by operator Stop")
        if fenced:
            self._finish_fenced_turn(fenced_item, fence_reason)
            return turn_id
        self._emit("snapshot", self.snapshot())
        return turn_id

    def _work(self):
        while True:
            with self._turn_lock:
                if self._stopping:
                    return
                if not self._queue:
                    self._worker = None
                    return
                item = self._queue.pop(0)
                item["state"] = "running"
                item["started"] = time.time()
                self._active_turn = (item["id"], item["source"])
                admitted_generation = item["admitted_generation"]
            turn_id = item["id"]
            self._emit("activity", {"phase": "turn_start", "turn_id": turn_id,
                                    "source": item["source"]})
            try:
                if self.stop_generation_snapshot() != admitted_generation:
                    self._finish_unrun_item(item, "cancelled", CANCELLED_BY_STOP)
                else:
                    setup_issue = self.setup_issue()
                    if setup_issue:
                        # A settings save can make a queued turn unusable. Keep
                        # the accepted text as a durable canceled row.
                        self._finish_unrun_item(item, "error", "Setup needed: " + setup_issue)
                    else:
                        self._run_turn_item(item, admitted_generation)
            finally:
                self._finalize_intake_entry(item)
                with self._turn_lock:
                    self._active_turn = ("", "")
                self._emit("activity", {"phase": "turn_end", "turn_id": turn_id,
                                        "source": item["source"],
                                        "state": item["state"], "error": item["error"]})
                self._emit("snapshot", self.snapshot())

    def _run_turn_item(self, item, admitted_generation):
        try:
            self._register_available_model_key()
            if self.stop_generation_snapshot() != admitted_generation:
                self._finish_unrun_item(item, "cancelled", CANCELLED_BY_STOP)
                return
            with self.session_operation() as session, session._turn_lock:
                self._apply_memory_binding()
                reply = session.run_turn(
                    item["text"], item["meta"],
                    is_cancelled=lambda: self.stop_generation_snapshot() != admitted_generation)
            if session.last_turn_interrupted:
                self._finalize_intake_entry(item, "cancelled")
                with self._turn_lock:
                    item["state"] = "cancelled"
                    item["reply"] = reply
                    item["error"] = "Interrupted by the user."
                self.error = ""
            else:
                self._finalize_intake_entry(item, "done")
                with self._turn_lock:
                    item["state"] = "done"
                    item["reply"] = reply
                self.error = ""
        except Exception as exc:
            message = str(exc) if str(exc) else type(exc).__name__
            message = self.session.log.redact(message)
            if self.session.last_turn_interrupted:
                self._finalize_intake_entry(item, "cancelled")
                with self._turn_lock:
                    item["state"] = "cancelled"
                    item["error"] = "Interrupted by the user."
                self.error = ""
            else:
                self._finalize_intake_entry(item, "error")
                with self._turn_lock:
                    item["state"] = "error"
                    item["error"] = message
                self.error = message

    def turn(self, slug, turn_id):
        if slug != self.slug:
            raise ValueError("Unknown session: " + str(slug))
        with self._turn_lock:
            item = self._turns.get(turn_id)
            if item is None:
                raise ValueError("Unknown turn: " + str(turn_id))
            return {key: item.get(key) for key in ("id", "state", "reply", "error", "source", "created")}

    def busy(self):
        with self._turn_lock:
            if any(item["state"] in ("queued", "running") for item in self._turns.values()):
                return True
        return self.session._in_turn.is_set()

    def interrupt(self, slug=None):
        if slug is not None and slug != self.slug:
            raise ValueError("Unknown session: " + str(slug))
        with self._turn_lock:
            self.stop_generation += 1
            # Publish the Stop fence under the same lock that fences a queued
            # submit. A later human submit is ordered after this Stop and may
            # clear the fence; an earlier one is canceled below.
            self.operator_stopped = True
            self._save_operator_stop()
            active = None
            active_source = ""
            cancelled = []
            for item in self._turns.values():
                if item["state"] == "running":
                    active = item["id"]
                    active_source = item["source"]
                elif item["state"] == "queued":
                    item["state"] = "cancelled"
                    item["error"] = CANCELLED_BY_STOP
                    cancelled.append(item)
            self._queue = []
        for request in self.pending_approvals():
            self.resolve_approval(request["id"], False)
        self.session.interrupt("user")
        # Stop still cancels queued work, but the typed text is kept as a
        # durable user row with its request identity and cancelled status.
        self._persist_cancelled_turns(cancelled, CANCELLED_BY_STOP)
        meta = self._manager._metadata.get(self.slug) or {}
        meta["stopped"] = True
        self._manager._metadata[self.slug] = meta
        cancelled_ids = []
        for item in cancelled:
            cancelled_ids.append(item["id"])
        result = {"turn_id": active, "active_turn_id": active or "", "stopped": True,
                  "cancelled_turn_ids": cancelled_ids}
        self._emit("activity", {"phase": "stopped", "turn_id": active,
                                "source": active_source})
        self._emit("snapshot", self.snapshot())
        return result

    def _persist_cancelled_turns(self, items, reason):
        for item in items:
            self._persist_input(item, reason)

    def session_for(self, slug):
        if slug == self.slug:
            return self.session
        return None

    def _message_rows(self, message, turn_id, source):
        meta = message.meta or {}
        # A durable row owns its own turn identity. This keeps interrupted or
        # partial rows attached to their turn, not to whichever turn is newest.
        request_id = meta.get("request_id")
        if isinstance(request_id, str) and request_id:
            turn_id = request_id
        meta_source = meta.get("source")
        if isinstance(meta_source, str) and meta_source:
            source = meta_source
        base = {"turn_id": turn_id, "source": source,
                "automatic": bool(meta.get("wake") or source in ("autonomy", "wake", "timer", "floor", "task")),
                "response_id": meta.get("response_id")}
        rows = []
        if message.kind == "agent" and isinstance(meta.get("thought"), str) and meta["thought"].strip():
            rows.append(dict(base, id=str(message.i) + "-thought", kind="thought",
                             text=meta["thought"], timestamp=message.date, name=self._config.agent_name))
        kinds = {"user": "user", "agent": "agent", "note": "note", "work": "note",
                 "tool": "tool", "echo": "result"}
        kind = kinds.get(message.kind)
        if kind is None:
            return rows
        name = meta.get("name") or meta.get("agent") or ""
        if message.kind == "agent":
            name = self._config.agent_name
        if message.kind == "tool" and not name:
            try:
                name = json.loads(message.text).get("name", "")
            except (ValueError, TypeError):
                name = ""
        status = ""
        if message.kind == "agent":
            if meta.get("interrupted"):
                status = "stopped"
            elif meta.get("success") is False:
                status = "failed"
        elif message.kind == "user" and meta.get("cancelled"):
            status = "cancelled"
        attachment_paths = []
        if message.kind == "user":
            for attachment in meta.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                path = attachment.get("path")
                if isinstance(path, str) and Path(path).is_absolute():
                    attachment_paths.append(path)
        rows.append(dict(base, id=str(message.i), kind=kind, text=message.text,
                         timestamp=message.date, name=name, call_id=meta.get("call_id"), status=status,
                         attachments=attachment_paths, diffs=meta.get("diffs", []),
                         final=(message.kind == "agent" and not meta.get("tool_calls")
                                and not meta.get("partial"))))
        return rows

    def _history_rows(self, message, turn_id, source):
        rows = self._message_rows(message, turn_id, source)
        for row in rows:
            turn_id = presentation_id(row.get('turn_id'))
            response_id = presentation_id(row.get('response_id'))
            source = str(row.get('source') or '')[:256]
            if turn_id != (row.get('turn_id') or '') or response_id != (row.get('response_id') or '') or source != (row.get('source') or ''):
                row['truncated'] = True
            row['turn_id'] = turn_id
            row['response_id'] = response_id
            row['source'] = source
            row['record_i'] = message.i
            if len(json.dumps(row, ensure_ascii=False).encode('utf-8')) > 12000:
                preview = row['text'][:512]
                complete = row
                row = {}
                for key in ('id', 'kind', 'turn_id', 'source', 'response_id', 'final', 'status', 'record_i'):
                    row[key] = complete.get(key)
                row['text'] = preview
                row['truncated'] = True
            yield row

    def _history_identity(self, upto):
        with self._history_lock:
            turn_id, source = self._history_context[-1] if self._history_context else ('', '')
            while len(self._history_context) <= upto:
                message = self.session.log.get(len(self._history_context))
                meta = message.meta or {}
                if message.kind == 'user' and not meta.get('steer'):
                    turn_id = meta.get('request_id') or 'history-' + str(message.i)
                    source = meta.get('source') or ('autonomy' if meta.get('wake') else 'chat')
                self._history_context.append((turn_id or 'history', source))
            return self._history_context[upto]

    def history_page(self, before=None, limit=80):
        with self.session_operation():
            return self._history_page(before, limit)

    def _history_page(self, before=None, limit=80):
        total = self.session.log.count()
        if before is None:
            before = total
        if type(before) is not int or not 0 <= before <= total:
            raise ValueError('Invalid history cursor')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('History limit must be between 1 and 200')
        rows = []
        used = 512
        cursor = before
        while cursor > 0 and before - cursor < limit:
            index = cursor - 1
            message = self.session.log.get(index)
            turn_id, source = self._history_identity(index)
            group = list(self._history_rows(message, turn_id, source))
            size = len(json.dumps(group, ensure_ascii=False).encode('utf-8'))
            if used + size > 64000:
                break
            rows[0:0] = group
            used += size
            cursor = index
        return {'messages': rows, 'before': cursor, 'has_more': cursor > 0,
                'total': total, 'history_id': self.history_id}

    def history_record(self, index, offset=0):
        with self.session_operation():
            return self._history_record(index, offset)

    def _history_record(self, index, offset=0):
        if type(index) is not int or type(offset) is not int or offset < 0:
            raise ValueError('Invalid record location')
        total = self.session.log.record_size(index)
        if offset > total:
            raise ValueError('Record offset exceeds its size')
        chunk = self.session.log.record_bytes(index, offset, 16384)
        return {'data': base64.b64encode(chunk).decode('ascii'), 'total': total,
                'next_offset': offset + len(chunk), 'history_id': self.history_id}

    def gist_page(self, token=None, offset=0):
        with self.session_operation():
            return self._gist_page(token, offset)

    def _gist_page(self, token=None, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid summary offset')
        with self._history_lock:
            if token is None:
                if offset != 0:
                    raise ValueError('A new summary read starts at zero')
                token = uuid.uuid4().hex
                self._gist_snapshot = (token, self.session.view.render().encode('utf-8'))
            if self._gist_snapshot is None or self._gist_snapshot[0] != token:
                raise ValueError('Summary read expired; read the summaries again')
            data = self._gist_snapshot[1]
            if offset > len(data):
                raise ValueError('Summary offset exceeds its size')
            chunk = data[offset:offset + 16384]
            next_offset = offset + len(chunk)
            result = {'data': base64.b64encode(chunk).decode('ascii'), 'total': len(data),
                      'token': token, 'next_offset': next_offset, 'history_id': self.history_id}
            if next_offset == len(data):
                self._gist_snapshot = None
            return result

    def _refresh_layer_count(self):
        now = time.monotonic()
        with self._memory_lock:
            if self._memory_refreshing or now - self._layer_checked < 30.0:
                return
            self._layer_checked = now
            self._memory_refreshing = True
        threading.Thread(target=self._load_layer_count, name="goblin-memory-status", daemon=True).start()

    def _make_memory_client(self, settings):
        if settings.mode == 'embedded':
            return AetherMindClient(direct_client=DirectMcpClient(timeout=settings.timeout),
                                    read_only_projects=(), create_project=settings.create_project,
                                    recall_scan=settings.recall_scan)
        return AetherMindClient(url=settings.endpoint, token_env=settings.token_env,
                               timeout=settings.timeout, create_project=False,
                               recall_scan=settings.recall_scan)

    def _make_memory_capture(self, config, root):
        settings = config.aethermind
        bootstrap = {'status': 'external'}
        if settings.mode == 'embedded' and settings.create_project:
            from .bootstrap_seeds import ensure_bootstrap
            shelf = config.skills.shelf or self.home / 'skills'
            bootstrap = ensure_bootstrap(settings.project, shelf)
        client = self._make_memory_client(settings)
        default_project = str((self.home / 'aethermind').resolve())
        if settings.mode == 'embedded' and settings.project == default_project:
            outbox = root / 'outbox'
        else:
            identity = json.dumps([settings.mode, settings.endpoint, settings.project])
            outbox = root / 'memory-outboxes' / hashlib.sha256(identity.encode()).hexdigest()
        capture = Capture(client, outbox, settings.project, settings.personal_project,
                          settings.author, note_project=settings.note_project,
                          recall_limit=settings.recall_limit,
                          omit_outcomes=settings.capture_omit_outcomes,
                          inheritance_refresh_seconds=settings.inheritance_refresh_seconds)
        return client, capture, bootstrap

    def _apply_memory_binding(self):
        config = self._config
        if self._active_memory_settings == config.aethermind:
            return
        client, capture, bootstrap = self._make_memory_capture(config, self._history_anchor)
        payload = {'session_id': self.session.session_id, 'turn': self.session.turn,
                   'cwd': str(self.session.workspace.path), 'approval_policy': self.session.tools.approval}
        capture.session_start(payload)
        capture.turn_number = max(capture.turn_number, self.session.turn)
        self.capture.uninstall(self.session.hooks)
        with self._memory_lock:
            self.capture = capture
            self.memory = client
            self._active_memory_settings = config.aethermind
            self._bootstrap = bootstrap
        self.session.capture = capture
        capture.install(self.session.hooks)
        for name in ('memory_recall', 'memory_tree', 'memory_note'):
            self.session.tools._tools.pop(name, None)
        capture.register_tools(self.session.tools)
        with self._memory_lock:
            self._layer_count = 0
            self._layer_checked = 0.0
            self._memory_state = 'loading'

    def _apply_idle_memory_binding(self):
        if self.session._in_turn.is_set():
            return
        if not self.session._turn_lock.acquire(blocking=False):
            return
        try:
            self._apply_memory_binding()
        finally:
            self.session._turn_lock.release()

    def _load_layer_count(self):
        capture = self.capture
        try:
            summary = capture.client.call("status", {"project_root": capture.project_id})
            if not isinstance(summary, dict) or summary.get("success") is False:
                raise ValueError("Continuity status unavailable")
            with self._memory_lock:
                if self.capture is not capture:
                    return
                if isinstance(summary.get("layer_count"), int):
                    self._layer_count = summary["layer_count"]
                self._memory_state = "ready"
        except Exception:
            with self._memory_lock:
                if self.capture is capture:
                    self._memory_state = "unavailable"
        finally:
            with self._memory_lock:
                self._memory_refreshing = False
                if self.capture is capture:
                    self._layer_checked = time.monotonic()
            self._emit("snapshot", self.snapshot())

    def _memory_summary(self):
        self._refresh_layer_count()
        debt = 0
        try:
            debt = len(self.capture.debt())
        except Exception:
            debt = 0
        if self.memory is None:
            state = "unavailable"
        elif debt:
            state = "debt"
        else:
            state = self._memory_state
        return {"state": state, "debt": debt, "layers": self._layer_count,
                "project": self.capture.project_id, "mode": self._active_memory_settings.mode,
                "foundation": self._bootstrap,
                "pending_change": self._active_memory_settings != self._config.aethermind}

    def setup_issue(self):
        """Truthful readiness from the model and workspace settings."""
        issues = []
        with self._event_lock:
            config = self._config
            workspace_issue_text = self._workspace_issue
            settings_issue_text = self._settings_issue
        if settings_issue_text:
            issues.append(settings_issue_text)
        if workspace_issue_text:
            issues.append(workspace_issue_text)
        binding = config.model_binding()
        connection, endpoint, model, api_kind, command, _, _, _ = binding
        if api_kind == "cli_worker":
            if not str(command or "").strip():
                issues.append("Model is not configured: set a CLI command in Settings")
        else:
            if not endpoint or not model:
                if connection == "custom":
                    issues.append("Model is not configured: set a model endpoint and model in Settings")
                else:
                    issues.append("Model is not configured: select an account model in Connections")
        return "; ".join(issues)

    def active_turn_id(self):
        return self._active_turn[0]

    def _turn_rows(self):
        rows = []
        with self._turn_lock:
            selected = []
            for item in reversed(self._turns.values()):
                if len(selected) == 64:
                    break
                selected.append(item)
            active = self._turns.get(self.active_turn_id())
            if active is not None and active not in selected:
                selected[-1:] = [active]
            for item in reversed(selected):
                rows.append({
                    "id": presentation_id(item.get("id", "")),
                    "state": item.get("state", ""),
                    "source": str(item.get("source", ""))[:256],
                    "text": self.session.log.redact(item.get("text", ""))[:1024],
                    "error": self.session.log.redact(item.get("error", ""))[:1024],
                    "created": item.get("created", 0),
                })
        return rows

    def snapshot(self):
        with self._history_lock:
            return self._snapshot()

    def _snapshot(self):
        with self._event_lock:
            revision = self._revision
            settings_revision = self._settings_revision
            settings_issue = self._settings_issue
        config = self._config
        setup_issue = self.setup_issue()
        autonomy = bool(getattr(self._config, "autonomy", None) and self._config.autonomy.enabled)
        settings = config.settings_dict()
        try:
            world_view = peek_world_view(config)
            if world_view is not None:
                settings['world_view'] = world_view
                autonomy_settings = settings.get('advanced', {}).get('autonomy', {})
                if 'world_view' in autonomy_settings:
                    autonomy_settings['world_view'] = world_view
        except FloorDefinitionError as error:
            if not settings_issue:
                settings_issue = str(error)
        tools = []
        disabled = self.session.tools._disabled_names()
        for tool in self.session.tools._tools.values():
            origin = 'built-in'
            if tool.name in self.session._plugin_tools:
                origin = 'plugin'
            elif tool.name in self.session._host_tools:
                origin = 'host'
            classes = []
            for name, patterns in config.tools.approval_classes.items():
                for pattern in patterns:
                    if fnmatch.fnmatchcase(tool.name, pattern):
                        classes.append(name)
                        break
            rule = approval_rule(tool.name, config.tools.approval_classes, config.tools.approval_rules)
            tools.append({"name": tool.name, "description": tool.description,
                          "enabled": tool.name not in disabled, "origin": origin,
                          "risky": tool.risky, "approval_classes": classes, "approval_rule": rule})
        return {
            "revision": revision,
            "settings_revision": settings_revision,
            "history_count": self.session.log.count(),
            "history_id": self.history_id,
            "turns": self._turn_rows(),
            "active_turn_id": presentation_id(self.active_turn_id()),
            "ready": setup_issue == "",
            "setup_issue": setup_issue,
            "settings_issue": settings_issue,
            "busy": self.busy(),
            "model": config.chat_model(),
            "backend": config.model_binding()[3],
            "workspace": config.workspace,
            "compaction": self.session.compactor.status() if hasattr(self, "session") else {},
            "memory": self._memory_summary(),
            "autonomy": {"enabled": autonomy,
                         "last_stand_down": dict(
                             (getattr(self.autonomy, "state", {}) or {}).get("last_stand_down") or {})},
            "settings": settings,
            "tools": tools,
            "pending_approvals": self.pending_approvals(),
            "error": self.error,
        }

    def memory_inspect(self, connection=None):
        config = self._config
        if connection is not None:
            values = config.settings_dict()
            values['advanced']['aethermind'] = connection
            normalized = normalize_settings(values, config.manifest, self.home)
            config = Config(self.home, config.manifest, normalized)
        settings = config.aethermind if connection is not None else self._active_memory_settings
        if settings.mode == 'embedded':
            from .memory_cores import inspect_core
            return inspect_core(settings.project)
        client = self._make_memory_client(settings)
        capabilities = client.call('capabilities', {'project_root': settings.project})
        status = client.call('status', {'project_root': settings.project})
        issues = []
        for name, result in (('capabilities', capabilities), ('status', status)):
            if not isinstance(result, dict):
                issues.append({'file': name, 'message': 'Remote core returned an invalid report'})
            elif result.get('success') is False or result.get('error'):
                detail = result.get('error') or result.get('message') or 'Remote core reported failure'
                issues.append({'file': name, 'message': str(detail)})
        return {'mode': 'remote', 'project_root': settings.project,
                'compatible': not issues, 'healthy': not issues, 'issues': issues,
                'capabilities': capabilities, 'status': status}

    def memory_export(self, destination):
        with self._memory_lock:
            capture = self.capture
            mode = self._active_memory_settings.mode
        if mode != 'embedded':
            raise ValueError('Export requires an active local memory core')
        from .memory_cores import export_core
        return export_core(capture.project_id, destination)

    def memory_import(self, archive):
        from .memory_cores import import_core
        return import_core(archive, self.home / 'aethermind-cores')

    def memory_read(self):
        capture = self.capture
        result = {"status": None, "inheritance": "", "layers": None}
        try:
            summary = capture.client.call("status", {"project_root": capture.project_id})
            result["status"] = summary
            if isinstance(summary, dict):
                count = summary.get("layer_count", summary.get("count"))
                if self.capture is capture and isinstance(count, int):
                    self._layer_count = count
        except Exception as error:
            result["status"] = {"success": False, "error": type(error).__name__ + ": " + str(error)}
        try:
            result["inheritance"] = capture.inheritance_block()
        except Exception as error:
            result["inheritance"] = "Unavailable (" + type(error).__name__ + ")"
        try:
            result["layers"] = capture.client.read_layers(capture.project_id, last_n=20)
        except Exception as error:
            result["layers"] = {"success": False, "error": type(error).__name__ + ": " + str(error)}
        return result

    def _apply_autonomy_lifecycle(self, config, previous_config, admitted_stop_generation=None):
        """One lifecycle path for startup, settings saves and toolbar toggles."""
        autonomy = self.autonomy
        replaced = self._autonomy_storage_changed(config)
        if replaced:
            # Every closure and store must belong to the incoming setup owner.
            # The caller admits this change only after running work has settled.
            candidate = Autonomy(self._manager, clock=autonomy.clock)
            candidate._production_paused = autonomy._production_paused
            autonomy.close()
            self.autonomy = candidate
        self.autonomy.config = config
        self.autonomy.settings = config.autonomy
        self.autonomy.learning.pass_score = config.autonomy.lesson_pass_score
        self.autonomy.learning.max_open = config.autonomy.max_open_lessons
        self.autonomy.learning.fail_limit = config.autonomy.lesson_fail_limit
        self.autonomy.learning.floor_slug = config.autonomy.floor_slug
        if not config.autonomy.enabled:
            self.autonomy.close()
            return
        thread = self.autonomy.thread
        if thread is not None and not thread.is_alive():
            self.autonomy.thread = None
        if not replaced and previous_config is not None and not previous_config.autonomy.enabled:
            # A real off-to-on transition restarts Autonomy. Do not clear a
            # Stop that arrived while this save was being applied; check and
            # clear under the same lock that publishes a Stop.
            with self._turn_lock:
                if admitted_stop_generation == self.stop_generation:
                    self.clear_operator_stop()
        self.autonomy.start()

    def _apply_saved_settings(self, saved, previous_config, admitted_stop_generation):
        """Apply a committed document, reporting the first runtime failure."""
        steps = [
            ("memory", self._apply_idle_memory_binding),
            ("session", lambda: self.session.apply_config(saved)),
            ("voice", lambda: self.voice.refresh_settings()),
            ("manager", lambda: self._manager.refresh_config()),
            ("autonomy", lambda: self._apply_autonomy_lifecycle(
                saved, previous_config, admitted_stop_generation)),
            ("tools", lambda: self.session.attach_host_tools(self)),
        ]
        issue = ""
        for label, step in steps:
            try:
                step()
            except Exception as error:
                if not issue:
                    detail = self.session.log.redact(str(error))
                    issue = ("Settings were saved but " + label + " apply failed ("
                             + type(error).__name__ + ": " + detail
                             + "). Repair the reported setting and save settings again to retry.")
        with self._backend_lock:
            self._backend_cache = None
        with self._event_lock:
            self._settings_issue = issue
        return issue

    def _validate_changed_local_memory_target(self, values):
        candidate = Config(self.home, self._config.manifest, values).aethermind
        current = self._config.aethermind
        if candidate.mode != 'embedded' or candidate.create_project:
            return
        if (candidate.mode, candidate.project) == (current.mode, current.project):
            return
        from .memory_cores import inspect_core
        report = inspect_core(candidate.project)
        if not report['store_exists'] or not report['compatible'] or not report['healthy']:
            raise ConfigError('Selected local memory core cannot be activated: '
                              + json.dumps(report['issues'], ensure_ascii=False))

    def _autonomy_storage_scope(self, config):
        from .autonomy import _home_path
        from .floor_definition import definition_path
        return (_home_path(config.home, config.autonomy.state_file).resolve(),
                _home_path(config.home, config.autonomy.tasks_dir).resolve(),
                _home_path(config.home, config.autonomy.lesson_file).resolve(),
                definition_path(config).resolve())

    def _autonomy_storage_changed(self, config):
        from .floor_definition import definition_path
        autonomy = self.autonomy
        current = (autonomy.path.resolve(), autonomy.tasks_dir.resolve(),
                   autonomy.learning.path.resolve(), definition_path(autonomy.config).resolve())
        return current != self._autonomy_storage_scope(config)

    def save_settings(self, settings, expected_revision=None, *, floor_definition=None,
                      autonomy_switch='hold'):
        with self.session_operation():
            with self._settings_lock:
                values = normalize_settings(settings, self._config.manifest, self.home)
                self._hold_autonomy_switch(values, autonomy_switch)
                candidate = Config(self.home, self._config.manifest, values)
                owned_maintenance = self._maintenance and self._maintenance_owner == threading.get_ident()
                if not self._autonomy_storage_changed(candidate) or owned_maintenance:
                    return self._save_settings(values, expected_revision, floor_definition=floor_definition)
        # Acquire maintenance only after releasing the operation pin and settings
        # lock. Its idle check admits no competing session work before commit.
        with self.idle_maintenance():
            return self._save_settings(values, expected_revision, floor_definition=floor_definition)

    def _hold_autonomy_switch(self, values, mode):
        """The autonomy switch belongs to the operator.

        `operator` is the settings form and accepts the incoming value, `close`
        is a declared stand-down and can only turn autonomy off, and `hold` is
        every other save: recalling a template or applying a floor definition
        leaves the switch exactly where the operator put it.
        """
        if mode == 'operator':
            return
        advanced = values.get("advanced")
        if not isinstance(advanced, dict):
            advanced = {}
            values["advanced"] = advanced
        autonomy = advanced.get("autonomy")
        if mode == 'close':
            if not isinstance(autonomy, dict):
                autonomy = {}
                advanced["autonomy"] = autonomy
            autonomy["enabled"] = False
            return
        # `hold`: carry the operator's current switch state. A save that names no
        # autonomy settings and leaves autonomy off needs no switch field at all.
        if not self._config.autonomy.enabled:
            if isinstance(autonomy, dict) and 'enabled' in autonomy:
                autonomy["enabled"] = False
            return
        if not isinstance(autonomy, dict):
            autonomy = {}
            advanced["autonomy"] = autonomy
        autonomy["enabled"] = True

    def _save_settings(self, settings, expected_revision=None, *, floor_definition=None):
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ValueError("expected revision must be an integer")
        with self._settings_lock:
            with self._event_lock:
                current_revision = self._settings_revision
            if expected_revision is not None and expected_revision != current_revision:
                raise RuntimeError("Settings changed since this form was loaded")
            admitted_stop_generation = self.stop_generation_snapshot()
            values = normalize_settings(settings, self._config.manifest, self.home)
            self._validate_changed_local_memory_target(values)
            incoming_floor = None
            if floor_definition is not None:
                incoming_floor = validate_definition(floor_definition)
                exercise_definition(incoming_floor)
            document_world_view = peek_world_view(self._config) if incoming_floor is None else None
            autonomy_settings = values.get('advanced', {}).get('autonomy', {})
            previous_autonomy = self._config.advanced.get('autonomy', {})
            prior_world_view = self._config.world_view
            prior_autonomy_view = previous_autonomy.get('world_view', prior_world_view)
            if expected_revision is not None and document_world_view is not None:
                prior_world_view = document_world_view
            if incoming_floor is not None:
                world_view = incoming_floor['compose']['world_view']
            elif values['world_view'] != prior_world_view:
                world_view = values['world_view']
            elif ('world_view' in autonomy_settings
                  and autonomy_settings['world_view'] != prior_autonomy_view):
                world_view = autonomy_settings['world_view']
            elif document_world_view is not None:
                world_view = document_world_view
            else:
                world_view = autonomy_settings.get('world_view', values['world_view'])
            values['world_view'] = world_view
            if 'world_view' in autonomy_settings:
                autonomy_settings['world_view'] = world_view
            staged_floor = incoming_floor if incoming_floor is not None else staged_world_view(self._config, world_view)
            saved = write_settings(self.home, self.manifest_path, values)
            saved_issue = workspace_issue(saved.workspace, create=False)
            with self._event_lock:
                previous_config = self._config
                self._config = saved
                self._workspace_issue = saved_issue
                self._revision += 1
                self._settings_revision = current_revision + 1
                # Readiness stays false until every runtime consumer accepts
                # the committed document.
                self._settings_issue = "Settings were saved and are being applied."
            floor_issue = ''
            if staged_floor is not None:
                try:
                    save_definition(saved, staged_floor)
                except Exception as error:
                    detail = self.session.log.redact(str(error))
                    floor_issue = ('Settings were saved but floor definition apply failed ('
                                   + type(error).__name__ + ': ' + detail + ').')
            self._apply_saved_settings(saved, previous_config, admitted_stop_generation)
            if floor_issue:
                with self._event_lock:
                    prior_issue = self._settings_issue
                    self._settings_issue = (prior_issue + ' ' if prior_issue else '') + floor_issue
            snapshot = self.snapshot()
        self._emit("snapshot", snapshot)
        return snapshot

    def execute_tool(self, name, arguments):
        with self.session_operation():
            return self._execute_tool(name, arguments)

    def _execute_tool(self, name, arguments):
        if not isinstance(name, str) or not name:
            raise ValueError("tool name is required")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return self.session.execute_tool(name, arguments)

    def voice_start(self):
        with self.session_operation():
            return self._voice_start()

    def _voice_start(self):
        self.voice.refresh_settings()
        return self.voice.start(self.slug)

    def voice_check(self):
        if self._stopping:
            raise RuntimeError("Application is shutting down")
        self.voice.refresh_settings()
        try:
            minted = self.voice_client.mint()
            self.session.log.add_secrets([self.voice_client._secret, minted["value"]])
            return {"ok": True, "auth": self._config.voice_auth,
                    "expires_at": minted["expires_at"], "message": "Voice authentication verified"}
        except VoiceError as exc:
            return {"ok": False, "auth": self._config.voice_auth,
                    "error_category": exc.category,
                    "message": "Voice authentication failed. Check the selected xAI connection."}

    def voice_events(self, lease_id, events):
        result = self.voice.events(self.slug, lease_id, events)
        if result.get("messages"):
            self._emit("snapshot", self.snapshot())
        return result

    def voice_stop(self, lease_id):
        return self.voice.stop(self.slug, lease_id)

    def voice_heartbeat(self, lease_id):
        return self.voice.heartbeat(self.slug, lease_id)

    def account_start(self, provider):
        if self._stopping:
            raise RuntimeError("Application is shutting down")
        return self.accounts.start(provider)

    def credential_authorize(self, role, provider="", endpoint=""):
        if self._stopping:
            raise RuntimeError("Application is shutting down")
        if not self._credential_authorization_lock.acquire(blocking=False):
            raise RuntimeError("A Keychain authorization is already open")
        try:
            if role == "account":
                self.accounts.definition(provider)
                store = self.accounts.stores[provider]
                state = store.keychain.authorize(store.service)
            elif role in ("model", "voice"):
                service = self._config.manifest["keychain_services"][role]
                account = "default"
                if role == "model":
                    account = canonical_model_endpoint(endpoint)
                    if not account:
                        raise ValueError("Select a model endpoint first")
                state = self.secret_provider.keychain.authorize(service, account)
            else:
                raise ValueError("Unknown credential role")
            message = "Keychain access authorized."
            if state == "missing":
                message = "No saved credential for this selection."
            return {"ok": state == "authorized", "message": message}
        finally:
            self._credential_authorization_lock.release()

    def account_status(self, provider):
        return self.accounts.status(provider)

    def account_list(self):
        return {"accounts": self.accounts.list()}

    def account_cancel(self, provider):
        result = self.accounts.cancel(provider)
        with self._account_checks_lock:
            checks = list(self._account_checks.values())
        for checked_provider, transport in checks:
            if checked_provider == provider:
                transport.cancel()
        return result

    def account_check(self, provider):
        definition = self.accounts.definition(provider)
        check_id = uuid.uuid4().hex
        transport = CancellableHTTP(_NoRedirect())
        with self._account_checks_lock:
            if self._stopping:
                raise RuntimeError("Application is shutting down")
            self._account_checks[check_id] = (provider, transport)
        try:
            credential = self._account_credential(provider)
            headers = {"Accept": "application/json",
                       "Authorization": "Bearer " + credential.value}
            headers.update(credential.headers)
            url = definition["model_base_url"].rstrip("/") + definition["models_path"]
            req = request.Request(url, headers=headers, method="GET")
            transport.check_cancelled()
            with transport.opener.open(req, timeout=15.0) as response:
                transport.response = response
                raw = response.read(1048577)
            transport.check_cancelled()
            if len(raw) > 1048576:
                raise ValueError("Model catalog exceeds the response limit")
            payload = json.loads(raw)
            models = self._account_models(payload)
            return {"provider": provider, "ok": True, "models": models,
                    "message": "Connected" if models else "Connected. No models available."}
        except (OAuthError, KeychainError):
            message = "Sign in required. Connect this account in Connections."
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                message = "Sign-in was rejected. Reconnect this account."
            elif status == 403:
                message = "The provider denied model access for this account."
            elif status == 429:
                message = "The provider reports an account limit. Try again later."
            else:
                message = "Provider connection failed (HTTP " + str(status) + ")."
        except CancelledIO:
            message = "Connection check cancelled."
        except (OSError, ValueError):
            message = "The provider connection or model catalog could not be read."
        finally:
            transport.cancel()
            with self._account_checks_lock:
                self._account_checks.pop(check_id, None)
        return {"provider": provider, "ok": False, "models": [], "message": message}

    @staticmethod
    def _account_models(payload):
        if not isinstance(payload, dict):
            raise ValueError("Model catalog must be an object")
        rows = payload.get("models", payload.get("data"))
        if not isinstance(rows, list):
            raise ValueError("Model catalog must contain models")
        models = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Model entry must be an object")
            model_id = row.get("id") or row.get("slug")
            if not isinstance(model_id, str) or not model_id:
                raise ValueError("Model entry needs an identifier")
            if model_id in seen:
                continue
            seen.add(model_id)
            name = row.get("display_name") or row.get("name") or model_id
            models.append({"id": model_id, "name": str(name)})
        return models

    def set_autonomy(self, enabled, expected_revision=None, actor='human'):
        if not isinstance(enabled, bool):
            raise ValueError("autonomy enabled must be true or false")
        if actor != 'human' and enabled:
            # A stand-down path may close the switch. Only the operator opens it.
            raise ValueError("Only the operator can start autonomy")
        # Read and write under the settings lock so an Autonomy toggle cannot
        # overwrite a settings save that completed in between. Lifecycle and
        # fence clearing belong to save_settings so there is one start path.
        with self._settings_lock:
            values = self._config.settings_dict()
            advanced = dict(values.get("advanced") or {})
            autonomy = dict(advanced.get("autonomy") or {})
            autonomy["enabled"] = enabled
            advanced["autonomy"] = autonomy
            values["advanced"] = advanced
            return self.save_settings(values, expected_revision=expected_revision,
                                      autonomy_switch='operator' if actor == 'human' else 'close')

    def floor_read(self):
        error = ""
        try:
            definition = load_definition(self._config)
        except FloorDefinitionError as exc:
            definition = default_definition()
            definition["source"] = "refused"
            definition["rules"] = []
            error = str(exc)
        status = self._floor_status(definition, error)
        return {"definition": export_text(definition), "status": status}

    def floor_save(self, text, expected_revision=None):
        with self.session_operation():
            return self._floor_save(text, expected_revision)

    def _floor_save(self, text, expected_revision=None):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("floor definition cannot be empty")
        parsed = import_text(text)
        with self._settings_lock:
            if expected_revision is not None:
                if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                    raise ValueError('expected revision must be an integer')
                with self._event_lock:
                    current_revision = self._settings_revision
                if expected_revision != current_revision:
                    raise RuntimeError('Settings changed since this floor editor was loaded')
            stored = save_definition(self._config, parsed)
        return {"definition": export_text(stored), "status": self._floor_status(stored, "")}

    def _floor_status(self, definition, error):
        now = time.time()
        interval = float(self._config.autonomy.self_prompt_gap_seconds or self._config.floor_interval_seconds)
        wakes = list((self.autonomy.state or {}).get("wakes") or [])
        last_wake = 0.0
        for item in wakes:
            if isinstance(item, dict):
                last_wake = max(last_wake, float(item.get("at") or 0))
        return {
            "enabled": bool(self._config.autonomy.enabled),
            "interval_seconds": interval,
            "last_wake": last_wake,
            "wakes_last_hour": sum(1 for item in wakes if isinstance(item, dict) and now - float(item.get("at") or 0) < 3600),
            "wakes_last_day": len(wakes),
            "fenced_seconds": 0.0 if not self.operator_stopped else -1.0,
            "operator_stopped": bool(self.operator_stopped),
            "floor_source": definition.get("source", ""),
            "rules": len(definition.get("rules") or []),
            "definition_error": error,
            "last_error": "",
            "session": self.slug,
            "counts": {
                "timers": len((self.autonomy.state or {}).get("timers") or {}),
                "tasks": len((self.autonomy.state or {}).get("tasks") or {}),
                "held": sum(len(v) for v in ((self.autonomy.state or {}).get("held") or {}).values()),
            },
        }

    def begin_shutdown(self):
        """Fence intake immediately, without changing the operator's Stop setting."""
        with self._turn_lock:
            if self._stopping:
                return
            self._stopping = True
            self.stop_generation += 1
        self.session.interrupt("shutdown")
        self.accounts.cancel_all()
        with self._account_checks_lock:
            checks = list(self._account_checks.values())
        for _, transport in checks:
            transport.cancel()

    def shutdown(self):
        self.begin_shutdown()
        for request in self.pending_approvals():
            self.resolve_approval(request["id"], False)
        try:
            self.autonomy.close()
        except Exception:
            pass
        self.voice.close()
        try:
            self.session.interrupt("user")
        except Exception:
            pass
        with self._turn_lock:
            cancelled = []
            for item in self._turns.values():
                if item["state"] == "queued":
                    item["state"] = "cancelled"
                    item["error"] = CANCELLED_BY_SHUTDOWN
                    cancelled.append(item)
            self._queue = []
        self._persist_cancelled_turns(cancelled, CANCELLED_BY_SHUTDOWN)
        # A running turn keeps its journal entry until session.close() returns.
        # The next launch recovers it only if its raw row never became durable.
        self.session.close()
        # Do not return while a turn worker can still write into the home
        # directory. A late write after the caller removes that directory
        # loses accepted input and raises from a dead-looking shutdown.
        worker = self._worker
        if isinstance(worker, threading.Thread) and worker.is_alive():
            worker.join(timeout=5.0)
            if worker.is_alive():
                print("goblin: turn worker still running at shutdown", file=sys.stderr, flush=True)

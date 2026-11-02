#!/usr/bin/env python3
"""Goblin NDJSON bridge.

stdout carries protocol frames only. stderr carries bounded diagnostics.
Slow work is dispatched off the reader, so Stop, approvals and snapshots stay
reachable. Chat intake runs on one owned FIFO lane in arrival order; each frame
carries the Stop generation stamped when it was read, so a later Stop fences
earlier intake instead of erasing it.
"""

import argparse
from collections import deque
import json
import os
import sys
import threading
import time
import traceback

from goblin_core.host import GoblinHost

PROTOCOL_LOCK = threading.Lock()

INTAKE_METHODS = ("chat/send", "chat/queue", "chat/steer")
SLOW_METHODS = ("bootstrap", "snapshot", "history/page", "history/record", "history/gist", "tool/execute", "voice/start", "voice/events", "voice/check", "account/start",
                "account/status", "account/list", "account/cancel", "account/check", "credentials/authorize",
                "memory/read", "memory/inspect", "memory/export", "memory/import", "settings/save", "autonomy/set",
                "floor/read", "floor/save", "memory/create", "history/list", "history/rotate", "history/select",
                "autonomy/templates", "autonomy/template/save", "autonomy/template/select", "autonomy/reset")


def write_frame(frame):
    line = json.dumps(frame, ensure_ascii=False, allow_nan=False)
    with PROTOCOL_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Bridge:
    def __init__(self, home, manifest, *, application_home=None):
        self.host = GoblinHost(home, manifest, emit=self.emit, application_home=application_home)
        self.stopping = threading.Event()
        self._shutdown_done = False
        self._active = 0
        self._active_lock = threading.Condition()
        self._intake = deque()
        self._intake_condition = threading.Condition()
        self._intake_thread = None
        self._intake_stopping = False
        self._intake_pending = 0

    def emit(self, event, data):
        if event not in ("snapshot", "stream", "activity", "approval", "message", "compaction"):
            return
        write_frame({"event": event, "data": data})

    def _redact(self, text):
        try:
            return self.host.session.log.redact(str(text))
        except Exception:
            return str(text)

    def _fail(self, request_id, message):
        write_frame({"id": request_id, "error": {"message": message}})

    def dispatch(self, request, generation=None, counted=False):
        if not counted:
            with self._active_lock:
                self._active += 1
        try:
            self._dispatch(request, generation)
        finally:
            with self._active_lock:
                self._active -= 1
                self._active_lock.notify_all()

    def _dispatch(self, request, generation=None):
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            request_id = ""
        method = request.get("method")
        params = request.get("params")
        if not isinstance(method, str) or not method:
            self._fail(request_id, "method is required")
            return
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self._fail(request_id, "params must be an object")
            return
        try:
            result = self.handle(method, params, generation)
        except Exception as error:
            message = self._redact(str(error) if str(error) else type(error).__name__)
            print("goblin: " + method + " failed: " + type(error).__name__ + ": " + message[:1000],
                  file=sys.stderr, flush=True)
            if method != "shutdown":
                self._fail(request_id, message)
            return
        write_frame({"id": request_id, "result": result})

    def handle(self, method, params, stop_generation=None):
        host = self.host
        if method == "bootstrap":
            return host.snapshot()
        if method == "snapshot":
            return host.snapshot()
        if method == "history/page":
            return host.history_page(params.get("before"), params.get("limit", 80))
        if method == "history/record":
            return host.history_record(params.get("i"), params.get("offset", 0))
        if method == "history/gist":
            return host.gist_page(params.get("token"), params.get("offset", 0))
        if method == "history/list":
            return host.history_list()
        if method == "history/rotate":
            return host.history_rotate()
        if method == "history/select":
            return host.history_select(params.get("id"))
        if method == "memory/create":
            from goblin_core.memory_cores import create_core
            return create_core(host.home / "aethermind-cores", seeded=params.get("seeded"),
                               skills_root=host.config.skills.shelf or host.home / "skills")
        if method in ("autonomy/templates", "autonomy/template/save", "autonomy/template/select", "autonomy/reset"):
            from goblin_core.autonomy_setups import AutonomySetups
            setups = AutonomySetups(host)
            revision = params.get("expected_revision")
            if method == "autonomy/templates":
                return setups.list()
            if method == "autonomy/template/save":
                return setups.save(params.get("name"), expected_revision=revision)
            if method == "autonomy/template/select":
                return setups.select(params.get("id"), expected_revision=revision)
            return setups.reset(expected_revision=revision)
        if method == "chat/send":
            turn_id = self._submit_text(host, params, stop_generation)
            return {"turn_id": turn_id}
        if method == "chat/queue":
            turn_id = self._submit_text(host, params, stop_generation)
            return {"turn_id": turn_id, "queued": True}
        if method == "chat/steer":
            text = params.get("text")
            if not isinstance(text, str):
                raise ValueError("chat/steer text must be text")
            if stop_generation is not None and stop_generation != host.stop_generation_snapshot():
                return {"accepted": False, "fenced": True}
            return {"accepted": host.session.steer(text)}
        if method == "chat/stop":
            return host.interrupt(host.slug)
        if method == "settings/save":
            settings = params.get("settings")
            if not isinstance(settings, dict):
                raise ValueError("settings/save needs a settings object")
            expected_revision = params.get("expected_revision")
            # This method is the operator's settings form. It is the one save that
            # may open the autonomy switch.
            return host.save_settings(settings, expected_revision=expected_revision,
                                      autonomy_switch='operator')
        if method == "approval/resolve":
            return host.resolve_approval(params.get("id"), params.get("accepted"))
        if method == "autonomy/set":
            enabled = params.get("enabled")
            return host.set_autonomy(enabled, expected_revision=params.get("expected_revision"))
        if method == "floor/read":
            return host.floor_read()
        if method == "floor/save":
            definition = params.get("definition")
            if not isinstance(definition, str):
                raise ValueError("floor/save definition must be text")
            return host.floor_save(definition, expected_revision=params.get("expected_revision"))
        if method == "tool/execute":
            return host.execute_tool(params.get("name"), params.get("arguments") or {})
        if method == "memory/read":
            return host.memory_read()
        if method == "memory/inspect":
            return host.memory_inspect(params.get('connection'))
        if method == "memory/export":
            return host.memory_export(params.get('destination'))
        if method == "memory/import":
            return host.memory_import(params.get('archive'))
        if method == "voice/start":
            return host.voice_start()
        if method == "voice/check":
            return host.voice_check()
        if method == "voice/events":
            lease_id = params.get("lease_id") or params.get("voice_id")
            if not isinstance(lease_id, str) or not lease_id:
                raise ValueError("voice/events needs lease_id")
            events = params.get("events")
            if not isinstance(events, list):
                raise ValueError("voice/events events must be an array")
            return host.voice_events(lease_id, events)
        if method == "voice/stop":
            lease_id = params.get("lease_id") or params.get("voice_id")
            if not isinstance(lease_id, str) or not lease_id:
                raise ValueError("voice/stop needs lease_id")
            return host.voice_stop(lease_id)
        if method == "voice/heartbeat":
            lease_id = params.get("lease_id") or params.get("voice_id")
            if not isinstance(lease_id, str) or not lease_id:
                raise ValueError("voice/heartbeat needs lease_id")
            return host.voice_heartbeat(lease_id)
        if method == "account/start":
            return host.account_start(params.get("provider") or "xai")
        if method == "account/status":
            return host.account_status(params.get("provider") or "xai")
        if method == "account/list":
            return host.account_list()
        if method == "account/cancel":
            return host.account_cancel(params.get("provider") or "xai")
        if method == "account/check":
            return host.account_check(params.get("provider") or "xai")
        if method == "credentials/authorize":
            return host.credential_authorize(params.get("role"), params.get("provider", ""),
                                             params.get("endpoint", ""))
        if method == "shutdown":
            # Fence new work before acknowledging; owned cleanup follows.
            self.host.begin_shutdown()
            self.stopping.set()
            return {"closed": True}
        raise ValueError("Unknown method: " + method)

    def _submit_text(self, host, params, stop_generation):
        text = params.get("text")
        if not isinstance(text, str):
            raise ValueError("chat text must be text")
        attachments = host._ingest_attachments(params.get("attachments"))
        if not text.strip() and not attachments:
            raise ValueError("chat needs text or attachments")
        if not text.strip():
            text = "See the attached file." if len(attachments) == 1 else "See the attached files."
        return host.submit(host.slug, text, source="chat", attachments=attachments,
                           client_request_id=params.get("request_id"),
                           expected_stop_generation=stop_generation,
                           expected_history_id=params.get("expected_history_id"))

    def _enqueue_intake(self, request):
        generation = self.host.stop_generation_snapshot()
        with self._intake_condition:
            self._intake.append((request, generation))
            with self._active_lock:
                self._intake_pending += 1
                self._active_lock.notify_all()
            if self._intake_thread is None or not self._intake_thread.is_alive():
                self._intake_thread = threading.Thread(target=self._intake_loop,
                                                       name="goblin-intake", daemon=True)
                self._intake_thread.start()
            self._intake_condition.notify_all()

    def _intake_loop(self):
        while True:
            with self._intake_condition:
                while not self._intake and not self._intake_stopping:
                    self._intake_condition.wait()
                if not self._intake:
                    return
                request, generation = self._intake.popleft()
                self._intake_condition.notify_all()
            with self._active_lock:
                self._active += 1
                self._intake_pending -= 1
                self._active_lock.notify_all()
            self.dispatch(request, generation, counted=True)

    def _stop_intake(self):
        with self._intake_condition:
            self._intake_stopping = True
            self._intake_condition.notify_all()
        thread = self._intake_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(10.0)
        # Drain any received frames the worker did not finish before the host
        # closes. The shutdown fence prevents them from starting fresh work.
        while True:
            with self._intake_condition:
                if not self._intake:
                    return
                request, generation = self._intake.popleft()
            with self._active_lock:
                self._intake_pending -= 1
                self._active_lock.notify_all()
            self.dispatch(request, generation)

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.stopping.set()
        self.host.begin_shutdown()
        self._stop_intake()
        try:
            self.host.shutdown()
        except Exception as error:
            print("goblin: shutdown failed: " + type(error).__name__ + ": " + self._redact(error),
                  file=sys.stderr, flush=True)

    def run(self):
        write_frame({"event": "activity", "data": {"phase": "ready"}})
        while not self.stopping.is_set():
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                write_frame({"id": "", "error": {"message": "Invalid JSON request frame"}})
                continue
            if not isinstance(request, dict):
                write_frame({"id": "", "error": {"message": "Request must be a JSON object"}})
                continue
            method = request.get("method")
            if method in INTAKE_METHODS:
                self._enqueue_intake(request)
            elif method in SLOW_METHODS:
                thread = threading.Thread(target=self.dispatch, args=(request,),
                                          name="goblin-request", daemon=True)
                thread.start()
            else:
                self.dispatch(request)
        self.host.begin_shutdown()
        self._wait_idle(10.0)
        self.shutdown()

    def _wait_idle(self, timeout):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._active_lock:
            while self._active > 0 or self._intake_pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_lock.wait(remaining)
        return True


def main(argv=None):
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--application-home")
    args = parser.parse_args(argv)
    try:
        bridge = Bridge(args.home, args.manifest, application_home=args.application_home)
    except Exception as error:
        print("goblin: startup failed: " + type(error).__name__ + ": " + str(error),
              file=sys.stderr, flush=True)
        return 2
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.shutdown()
    except Exception:
        print("goblin: bridge failed:\n" + traceback.format_exc(limit=4), file=sys.stderr, flush=True)
        bridge.shutdown()
        return 1
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Compaction route selection: its own backend and seat, or the chat route."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.aethermind import AetherMindClient
from goblin_core.aethermind_direct import DirectMcpClient
from goblin_core.capture import Capture
from goblin_core.config import default_settings, load_config, write_settings
from goblin_core.session import GoblinSession, same_model_route

LONG_PROMPT = "long context line. " * 100


class RecordingBackend:
    def __init__(self, reply, binding_key=None):
        self.reply = reply
        self.binding_key = binding_key
        self.calls = []
        self.options = []
        self.tool_arguments = []
        self.cancelled = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.on_delta = None
        self.cancel_event = None

    def chat(self, messages, tools=None, **options):
        self.calls.append(list(messages))
        self.options.append(dict(options))
        self.tool_arguments.append(tools)
        if not self.release.is_set():
            self.entered.set()
            self.release.wait(5)
        return {"content": self.reply, "tool_calls": [], "usage": {}, "finish_reason": "stop"}

    def cancel(self):
        self.cancelled.set()


def make_session(temp, chat_backend, factory=None, compactor_model=""):
    home = Path(temp)
    values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
    values["workspace"] = str(home / "workspace")
    values["model"] = "chat-model"
    values["model_base_url"] = "http://127.0.0.1:9/v1"
    advanced = dict(values.get("advanced") or {})
    compactor = dict(advanced.get("compactor") or {})
    compactor["workers"] = 1
    if compactor_model:
        compactor["model"] = compactor_model
    advanced["compactor"] = compactor
    values["advanced"] = advanced
    config = write_settings(home, ROOT / "product.json", values)
    Path(config.workspace).mkdir(parents=True, exist_ok=True)
    client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
    root = home / "chat" / config.slug
    capture = Capture(client, root / "outbox", config.aethermind.project, author="goblin")
    session = GoblinSession(lambda: config, lambda: chat_backend, root, capture=capture,
                            compactor_backend_factory=factory)
    return session, config


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class CompactorRouteTests(unittest.TestCase):
    def test_a_distinct_route_serves_summaries_from_its_own_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            summary = RecordingBackend("short summary", binding_key=("custom", "fast-compact"))
            session, _config = make_session(temp, chat, factory=lambda: summary,
                                            compactor_model="fast-compact")
            try:
                self.assertEqual(session.run_turn(LONG_PROMPT), "chat reply")
                self.assertTrue(wait_for(lambda: summary.calls))
                self.assertEqual(len(chat.calls), 1)
                self.assertEqual(summary.options[0]["tool_choice"], "none")
                self.assertEqual(summary.tool_arguments[0], [])
                self.assertIn("long context line.", summary.calls[0][-1]["content"])
            finally:
                session.close()

    def test_inherited_route_keeps_the_chat_seat(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            session, _config = make_session(temp, chat, factory=lambda: chat)
            try:
                with session.model_seat.use(object(), lambda: None):
                    worker = threading.Thread(
                        target=lambda: session.compactor.backend.chat(
                            [{"role": "user", "content": "summarize"}]))
                    worker.start()
                    time.sleep(0.3)
                    self.assertEqual(chat.calls, [])
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(chat.calls), 1)
            finally:
                session.close()

    def test_distinct_route_runs_while_the_foreground_seat_is_held(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            summary = RecordingBackend("short summary", binding_key=("custom", "fast-compact"))
            session, _config = make_session(temp, chat, factory=lambda: summary,
                                            compactor_model="fast-compact")
            try:
                with session.model_seat.use(object(), lambda: None):
                    worker = threading.Thread(
                        target=lambda: session.compactor.backend.chat(
                            [{"role": "user", "content": "summarize"}]))
                    worker.start()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(summary.calls), 1)
            finally:
                session.close()

    def test_compactor_cancel_does_not_cancel_the_chat_route(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            summary = RecordingBackend("short summary", binding_key=("custom", "fast-compact"))
            summary.release.clear()
            session, _config = make_session(temp, chat, factory=lambda: summary,
                                            compactor_model="fast-compact")
            holder = {}
            compactor_worker = None
            try:
                chat.release.clear()
                turn = threading.Thread(target=lambda: holder.setdefault(
                    "reply", session.run_turn("hello")))
                turn.start()
                self.assertTrue(chat.entered.wait(3))
                compactor_worker = threading.Thread(
                    target=lambda: session.compactor.backend.chat(
                        [{"role": "user", "content": "summarize"}]))
                compactor_worker.start()
                self.assertTrue(summary.entered.wait(3))
                session.compactor.backend.cancel()
                self.assertTrue(summary.cancelled.is_set())
                self.assertFalse(chat.cancelled.is_set())
                chat.release.set()
                turn.join(3)
                self.assertFalse(turn.is_alive())
                self.assertEqual(holder.get("reply"), "chat reply")
                summary.release.set()
                compactor_worker.join(3)
                self.assertFalse(compactor_worker.is_alive())
            finally:
                chat.release.set()
                summary.release.set()
                session.close()

    def test_the_factory_is_not_asked_until_compaction_needs_it(self):
        calls = []

        def broken():
            calls.append(True)
            raise RuntimeError("account is not connected")

        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            session, _config = make_session(temp, chat, factory=broken)
            try:
                self.assertEqual(calls, [])
                self.assertEqual(session.run_turn("hello"), "chat reply")
                self.assertEqual(calls, [])
            finally:
                session.close()

    def test_a_changed_route_is_read_live_without_rebuilding_the_session(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            first = RecordingBackend("first summary", binding_key=("custom", "first"))
            second = RecordingBackend("second summary", binding_key=("custom", "second"))
            routes = {"backend": first}
            session, _config = make_session(temp, chat, factory=lambda: routes["backend"],
                                            compactor_model="first")
            try:
                proxy = session.compactor.backend
                proxy.chat([{"role": "user", "content": "summarize"}])
                routes["backend"] = second
                proxy.chat([{"role": "user", "content": "summarize"}])
                self.assertEqual(len(first.calls), 1)
                self.assertEqual(len(second.calls), 1)
            finally:
                session.close()

    def test_apply_config_applies_while_the_foreground_seat_is_held(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = RecordingBackend("chat reply", binding_key=("custom", "chat-model"))
            session, _config = make_session(temp, chat, factory=lambda: chat)
            updated = load_config(Path(temp), ROOT / "product.json")
            updated.compactor.model = "fast-compact"
            finished = threading.Event()
            errors = []

            def apply():
                try:
                    session.apply_config(updated)
                except Exception as error:
                    errors.append(error)
                finished.set()

            try:
                with session.model_seat.use(object(), lambda: None):
                    worker = threading.Thread(target=apply)
                    worker.start()
                    self.assertTrue(finished.wait(2))
                    self.assertEqual(errors, [])
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(session.config.compactor.model, "fast-compact")
            finally:
                session.close()

    def test_route_identity_uses_identity_then_binding_keys(self):
        first = RecordingBackend("one", binding_key=("custom", "same"))
        second = RecordingBackend("two", binding_key=("custom", "same"))
        third = RecordingBackend("three", binding_key=("custom", "other"))
        bare = RecordingBackend("four")
        self.assertTrue(same_model_route(first, first))
        self.assertTrue(same_model_route(first, second))
        self.assertFalse(same_model_route(first, third))
        self.assertFalse(same_model_route(first, bare))


if __name__ == "__main__":
    unittest.main()

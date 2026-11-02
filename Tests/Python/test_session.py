"""Turn loop, tool registry and approval tests for the Goblin session."""

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
from goblin_core.config import default_settings, write_settings
from goblin_core.session import GoblinSession


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []
        self.on_delta = None
        self.cancelled = threading.Event()

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        if self.responses:
            return self.responses.pop(0)
        return {"content": "done", "tool_calls": [], "usage": {}, "finish_reason": "stop"}

    def cancel(self):
        self.cancelled.set()


class BlockingBackend(FakeBackend):
    def __init__(self):
        super().__init__([])
        self.entered = threading.Event()

    def chat(self, messages, tools=None, **options):
        self.entered.set()
        self.cancelled.wait(5)
        return {"content": "late answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"}


class FinalSteerBackend:
    """First reply waits for a steer, then completes as a final response."""

    def __init__(self):
        self.messages = []
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.on_delta = None

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        self.calls += 1
        if self.calls == 1:
            if self.on_delta is not None:
                self.on_delta("text", "first answer")
            self.entered.set()
            self.release.wait(5)
            return {"content": "first answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"}
        return {"content": "revised answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"}

    def cancel(self):
        self.release.set()


class StreamingBlockingBackend(BlockingBackend):
    """Stream a partial answer, then wait for interruption."""

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        if self.on_delta is not None:
            self.on_delta("text", "partial answer")
        self.entered.set()
        self.cancelled.wait(5)
        return {"content": "late answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"}


class FailingBackend(FakeBackend):
    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        raise RuntimeError("model failed")


class FinalThenFailBackend:
    def __init__(self):
        self.messages = []
        self.calls = 0

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        self.calls += 1
        if self.calls == 1:
            return {"content": "first answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"}
        raise RuntimeError("continuation failed")

    def cancel(self):
        pass


class FailingAfterReleaseBackend:
    """Accept a steer, then fail the turn at the next model boundary."""

    def __init__(self):
        self.messages = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        self.entered.set()
        self.release.wait(5)
        raise RuntimeError("transport failed")

    def cancel(self):
        self.release.set()


class HostStub:
    def __init__(self, config):
        self.config = config
        self.autonomy = None


def make_session(temp, backend, approve=None, delta_sink=None, advanced=None):
    home = Path(temp)
    values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
    values["workspace"] = str(home / "workspace")
    values["approval"] = "ask"
    if advanced is not None:
        values["advanced"] = advanced
    config = write_settings(home, ROOT / "product.json", values)
    Path(config.workspace).mkdir(parents=True, exist_ok=True)
    client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
    root = home / "chat" / config.slug
    capture = Capture(client, root / "outbox", config.aethermind.project, author="goblin")
    session = GoblinSession(lambda: config, lambda: backend, root, capture=capture,
                            approval=approve, delta_sink=delta_sink)
    return session, config


def durable_rows(session):
    """Raw log rows already fsynced to disk."""
    rows = []
    for path in sorted(session.log.root.glob("*.jsonl")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


class SessionTests(unittest.TestCase):
    def test_tool_execution_does_not_hold_the_model_seat(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "seat-call", "name": "seat_probe", "arguments": {}}]},
                {"content": "done", "tool_calls": []},
            ])
            session, _ = make_session(temp, backend, approve=lambda name, args: True)
            admitted = threading.Event()
            observed = []
            workers = []

            def tool():
                def background():
                    with session.model_seat.use(object(), lambda: None, background=True):
                        admitted.set()
                worker = threading.Thread(target=background, daemon=True)
                workers.append(worker)
                worker.start()
                observed.append(admitted.wait(1))
                return {"other_work_admitted": observed[-1]}

            session.tools.register("seat_probe", "Inspect model ownership", {"type": "object", "properties": {}}, tool)
            try:
                self.assertEqual(session.run_turn("Run the ownership check"), "done")
                self.assertEqual(observed, [True])
            finally:
                for worker in workers:
                    worker.join(2)
                session.close()

    def test_tool_call_runs_through_registry_and_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "write_file",
                                                 "arguments": {"path": "out.txt", "text": "proof"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "wrote the file", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend, approve=lambda name, args: True)
            self.assertEqual(session.run_turn("write it"), "wrote the file")
            self.assertEqual((Path(config.workspace) / "out.txt").read_text(), "proof")
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "tool", "echo", "agent"])
            echo = session.log.get(2)
            self.assertTrue(echo.meta["success"])
            self.assertEqual(echo.meta["name"], "write_file")
            session.close()

    def test_denied_tool_is_blocked_not_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "write_file",
                                                 "arguments": {"path": "out.txt", "text": "no"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "was blocked", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend, approve=lambda name, args: False)
            self.assertEqual(session.run_turn("write it"), "was blocked")
            self.assertFalse((Path(config.workspace) / "out.txt").exists())
            echo = session.log.get(2)
            self.assertTrue(echo.meta["blocked"])
            tool_message = backend.messages[-1][-1]
            self.assertIn("Blocked: The operator denied", tool_message["content"])
            session.close()

    def test_steer_is_drained_at_the_next_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "read_file",
                                                 "arguments": {"path": "seed.txt"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "used the steer", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend, approve=lambda name, args: True)
            (Path(config.workspace) / "seed.txt").write_text("seed")
            session.hooks.register("pre_tool", lambda payload: session.steer("also do the other thing"))
            self.assertEqual(session.run_turn("read it"), "used the steer")
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds.count("user"), 2)
            self.assertIn("[Steer from the user during this turn]", backend.messages[-1][-1]["content"])
            session.close()

    def test_steer_during_final_reply_is_consumed_in_the_same_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FinalSteerBackend()
            deltas = []
            session, _config = make_session(
                temp, backend, delta_sink=lambda kind, text: deltas.append((kind, text)))
            holder = {}
            errors = []

            def run():
                try:
                    holder["reply"] = session.run_turn("original prompt",
                                                       turn_meta={"request_id": "req-steer"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.steer("please also mention the invoice"))
            # Acceptance is durable before True: the raw row is already on
            # disk, on the current request, and appears exactly once.
            accepted_rows = durable_rows(session)
            steer_rows = [row for row in accepted_rows
                          if row["kind"] == "user" and row["meta"].get("steer")]
            self.assertEqual(len(steer_rows), 1)
            self.assertEqual(steer_rows[0]["text"], "please also mention the invoice")
            self.assertEqual(steer_rows[0]["meta"]["request_id"], "req-steer")
            backend.release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(holder["reply"], "revised answer")
            self.assertEqual(backend.calls, 2)
            self.assertIn("[Steer from the user during this turn] please also mention the invoice",
                          backend.messages[1][-1]["content"])
            self.assertEqual(backend.messages[1][-2],
                             {"role": "assistant", "content": "first answer"})
            rows = list(session.log.iter())
            self.assertEqual([row.kind for row in rows], ["user", "user", "agent", "agent"])
            self.assertEqual(rows[0].text, "original prompt")
            self.assertEqual(rows[1].text, "please also mention the invoice")
            self.assertTrue(rows[1].meta["steer"])
            self.assertEqual(rows[1].meta["request_id"], "req-steer")
            self.assertEqual(rows[2].text, "first answer")
            self.assertEqual(rows[2].meta["request_id"], "req-steer")
            self.assertEqual(rows[3].text, "revised answer")
            self.assertTrue(rows[2].meta["response_id"])
            self.assertTrue(rows[3].meta["response_id"])
            self.assertNotEqual(rows[2].meta["response_id"], rows[3].meta["response_id"])
            self.assertIn(("text", "first answer"), deltas)
            steer_rows = [row for row in rows if row.kind == "user" and row.meta.get("steer")]
            self.assertEqual(len(steer_rows), 1)
            self.assertEqual(session._steers, [])
            self.assertFalse(session.steer("late steer"))
            session.close()

    def test_accepted_steer_survives_a_failed_turn_and_a_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FailingAfterReleaseBackend()
            session, _config = make_session(temp, backend)
            errors = []

            def run():
                try:
                    session.run_turn("original prompt", turn_meta={"request_id": "req-fail-steer"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.steer("direction that must survive"))
            on_disk = [row for row in durable_rows(session)
                       if row["kind"] == "user" and row["meta"].get("steer")]
            self.assertEqual(len(on_disk), 1)
            backend.release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            rows = list(session.log.iter())
            steer_rows = [row for row in rows if row.kind == "user" and row.meta.get("steer")]
            self.assertEqual(len(steer_rows), 1)
            self.assertEqual(steer_rows[0].text, "direction that must survive")
            self.assertEqual(steer_rows[0].meta["request_id"], "req-fail-steer")
            self.assertEqual(rows[-1].kind, "agent")
            self.assertFalse(rows[-1].meta["success"])
            self.assertEqual(rows[-1].meta["request_id"], "req-fail-steer")
            session.close()

            reopened, _config = make_session(temp, FakeBackend([]))
            try:
                reopened_rows = [row for row in reopened.log.iter()
                                 if row.kind == "user" and row.meta.get("steer")]
                self.assertEqual(len(reopened_rows), 1)
                self.assertEqual(reopened_rows[0].text, "direction that must survive")
                self.assertEqual(reopened_rows[0].meta["request_id"], "req-fail-steer")
            finally:
                reopened.close()

    def test_accepted_steer_survives_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = BlockingBackend()
            session, _config = make_session(temp, backend, approve=lambda name, args: True)
            holder = {}
            errors = []

            def run():
                try:
                    holder["reply"] = session.run_turn("original prompt",
                                                       turn_meta={"request_id": "req-stop-steer"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.steer("keep this direction"))
            self.assertTrue(session.interrupt())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(holder["reply"], "Interrupted by the user.")
            rows = list(session.log.iter())
            self.assertEqual([row.kind for row in rows], ["user", "user", "agent"])
            self.assertEqual(rows[0].text, "original prompt")
            self.assertEqual(rows[1].text, "keep this direction")
            self.assertTrue(rows[1].meta["steer"])
            self.assertEqual(rows[1].meta["request_id"], "req-stop-steer")
            self.assertTrue(rows[2].meta["interrupted"])
            self.assertEqual(rows[2].meta["request_id"], "req-stop-steer")
            steer_rows = [row for row in rows if row.kind == "user" and row.meta.get("steer")]
            self.assertEqual(len(steer_rows), 1)
            self.assertFalse(session.steer("late steer"))
            session.close()

    def test_accepted_steer_does_not_discard_the_interrupted_partial_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = StreamingBlockingBackend()
            session, _config = make_session(temp, backend, delta_sink=lambda kind, text: None)
            holder = {}
            errors = []

            def run():
                try:
                    holder["reply"] = session.run_turn("original prompt",
                                                       turn_meta={"request_id": "req-steer-partial"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.steer("direction while streaming"))
            self.assertTrue(session.interrupt())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(holder["reply"], "Interrupted by the user.")
            rows = list(session.log.iter())
            self.assertEqual([row.kind for row in rows], ["user", "user", "agent", "agent"])
            self.assertTrue(rows[1].meta["steer"])
            self.assertEqual(rows[1].meta["request_id"], "req-steer-partial")
            self.assertEqual(rows[2].text, "partial answer")
            self.assertTrue(rows[2].meta["partial"])
            self.assertTrue(rows[2].meta["interrupted"])
            self.assertEqual(rows[2].meta["request_id"], "req-steer-partial")
            self.assertTrue(rows[3].meta["interrupted"])
            self.assertEqual(rows[3].meta["request_id"], "req-steer-partial")
            session.close()

    def test_steer_accepted_while_the_final_row_is_committing_is_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "first answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
                {"content": "revised answer", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, _config = make_session(temp, backend)
            accepted = []

            def steer_on_first_agent(message):
                if message.kind == "agent" and message.text == "first answer" and not accepted:
                    accepted.append(session.steer("include the invoice"))

            session.log.subscribe(steer_on_first_agent)
            self.assertEqual(session.run_turn("original prompt"), "revised answer")
            self.assertEqual(accepted, [True])
            self.assertEqual(backend.messages[1][-1]["content"],
                             "[Steer from the user during this turn] include the invoice")
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "agent", "user", "agent"])
            self.assertEqual(session._steers, [])
            session.close()

    def test_failure_after_a_steer_continuation_is_not_a_successful_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FinalThenFailBackend()
            session, _config = make_session(temp, backend)
            accepted = []

            def steer_on_first_agent(message):
                if message.kind == "agent" and message.text == "first answer" and not accepted:
                    accepted.append(session.steer("include the invoice"))

            session.log.subscribe(steer_on_first_agent)
            with self.assertRaises(RuntimeError):
                session.run_turn("original prompt")
            self.assertEqual(accepted, [True])
            self.assertFalse(session.last_turn_success)
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "agent", "user", "agent"])
            self.assertFalse(session.log.get(3).meta["success"])
            session.close()

    def test_every_authored_row_carries_the_turn_request_id(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "read_file",
                                                 "arguments": {"path": "seed.txt"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "read it", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend, approve=lambda name, args: True)
            (Path(config.workspace) / "seed.txt").write_text("seed")
            self.assertEqual(session.run_turn("read the seed", turn_meta={"request_id": "req-rows"}),
                             "read it")
            messages = list(session.log.iter())
            self.assertEqual([message.kind for message in messages], ["user", "tool", "echo", "agent"])
            for message in messages:
                self.assertEqual(message.meta.get("request_id"), "req-rows")
            session.close()

    def test_interrupted_partial_rows_keep_the_turn_request_id(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = StreamingBlockingBackend()
            session, _config = make_session(temp, backend, delta_sink=lambda kind, text: None)
            holder = {}
            errors = []

            def run():
                try:
                    holder["reply"] = session.run_turn("wait", turn_meta={"request_id": "req-interrupt"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.interrupt())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(holder["reply"], "Interrupted by the user.")
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "agent", "agent"])
            partial = session.log.get(1)
            self.assertTrue(partial.meta["partial"])
            self.assertTrue(partial.meta["interrupted"])
            self.assertEqual(partial.meta["request_id"], "req-interrupt")
            interruption = session.log.get(2)
            self.assertTrue(interruption.meta["interrupted"])
            self.assertEqual(interruption.meta["request_id"], "req-interrupt")
            session.close()

    def test_failed_turn_row_keeps_the_turn_request_id(self):
        with tempfile.TemporaryDirectory() as temp:
            session, _config = make_session(temp, FailingBackend([]))
            with self.assertRaises(RuntimeError):
                session.run_turn("fail", turn_meta={"request_id": "req-fail"})
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "agent"])
            failure = session.log.get(1)
            self.assertFalse(failure.meta["success"])
            self.assertEqual(failure.meta["request_id"], "req-fail")
            session.close()

    def test_plugin_computer_use_tools_survive_host_tool_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([])
            session, config = make_session(
                temp, backend, advanced={"computer_use": {"backend": "native"}})
            session.tools.register("computer_use__probe", "Plugin probe",
                                   {"type": "object", "properties": {}}, lambda: "ok")
            host = HostStub(config)
            session.attach_host_tools(host)
            self.assertIn("computer_use__probe", session.tools.names())
            self.assertIn("computer_list_apps", session.tools.names())
            first_host_tools = set(session._host_tools)
            session.attach_host_tools(host)
            self.assertIn("computer_use__probe", session.tools.names())
            self.assertEqual(set(session._host_tools), first_host_tools)
            names = session.tools.names()
            for name in first_host_tools:
                self.assertEqual(names.count(name), 1)
            self.assertEqual(session.tools.execute("computer_use__probe", {}), "ok")
            session.close()

    def test_session_reopens_after_view_json_is_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([{"content": "first reply", "tool_calls": [], "usage": {},
                                    "finish_reason": "stop"}])
            session, _config = make_session(temp, backend)
            self.assertEqual(session.run_turn("first prompt"), "first reply")
            root = session.root
            session.close()
            view_path = root / "view.json"
            self.assertTrue(view_path.exists())
            view_path.unlink()

            reopened, _config = make_session(
                temp, FakeBackend([{"content": "second reply", "tool_calls": [], "usage": {},
                                    "finish_reason": "stop"}]))
            self.assertEqual(reopened.log.count(), 2)
            self.assertIn("first prompt", reopened.view.render())
            self.assertIn("first reply", reopened.view.render())
            self.assertTrue(reopened.compactor.threads)
            self.assertEqual(reopened.run_turn("second prompt"), "second reply")
            self.assertIn("second prompt", reopened.view.render())
            reopened.close()

    def test_internal_worker_returns_a_report_to_the_one_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([{"content": "worker finished the job",
                                    "tool_calls": [], "usage": {}, "finish_reason": "stop"}])
            session, _config = make_session(temp, backend, approve=lambda name, args: True)
            started = session.agents.spawn("helper", "do one small job")
            self.assertEqual(started["status"], "started")
            reports = session.agents.wait_for_reports(10)
            self.assertEqual(reports["reports"][0]["report"], "worker finished the job")
            kinds = [message.kind for message in session.log.iter()]
            self.assertIn("work", kinds)
            session.close()

    def test_stop_accepted_before_turn_admission_is_not_erased(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([{"content": "must not run", "tool_calls": [],
                                    "usage": {}, "finish_reason": "stop"}])
            session, _config = make_session(temp, backend)
            session.interrupt()
            reply = session.run_turn("stop before start",
                                     turn_meta={"request_id": "req-preflight-stop"},
                                     is_cancelled=lambda: True)
            self.assertEqual(reply, "Interrupted by the user.")
            self.assertTrue(session.last_turn_interrupted)
            self.assertFalse(session.last_turn_success)
            self.assertTrue(session.tools.cancelled.is_set())
            self.assertEqual(backend.messages, [])
            rows = list(session.log.iter())
            self.assertEqual([row.kind for row in rows], ["user", "agent"])
            self.assertEqual(rows[0].text, "stop before start")
            self.assertEqual(rows[0].meta["request_id"], "req-preflight-stop")
            self.assertTrue(rows[0].meta["cancelled"])
            self.assertEqual(rows[0].meta["cancel_reason"], "Interrupted by the user.")
            self.assertTrue(rows[1].meta["interrupted"])
            self.assertEqual(rows[1].meta["request_id"], "req-preflight-stop")
            session.close()

    def test_is_cancelled_false_runs_the_admitted_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([{"content": "fresh reply", "tool_calls": [],
                                    "usage": {}, "finish_reason": "stop"}])
            session, _config = make_session(temp, backend)
            checks = []

            def is_cancelled():
                checks.append(True)
                return False

            reply = session.run_turn("fresh prompt",
                                     turn_meta={"request_id": "req-fresh"},
                                     is_cancelled=is_cancelled)
            self.assertEqual(reply, "fresh reply")
            self.assertEqual(checks, [True])
            self.assertEqual(len(backend.messages), 1)
            rows = list(session.log.iter())
            self.assertEqual([row.kind for row in rows], ["user", "agent"])
            self.assertEqual(rows[1].text, "fresh reply")
            self.assertEqual(rows[1].meta["request_id"], "req-fresh")
            session.close()

    def test_interrupt_waits_for_the_admission_cancellation_check(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([{"content": "must not run", "tool_calls": [],
                                    "usage": {}, "finish_reason": "stop"}])
            session, _config = make_session(temp, backend)
            checking = threading.Event()
            release = threading.Event()
            stop_called = threading.Event()
            interrupt_finished = threading.Event()
            holder = {}
            errors = []

            def is_cancelled():
                checking.set()
                release.wait(3)
                return True

            def run():
                try:
                    holder["reply"] = session.run_turn("racing stop", is_cancelled=is_cancelled)
                except Exception as exc:
                    errors.append(exc)

            def stop():
                stop_called.set()
                session.interrupt()
                interrupt_finished.set()

            runner = threading.Thread(target=run)
            stopper = threading.Thread(target=stop)
            runner.start()
            self.assertTrue(checking.wait(3))
            stopper.start()
            self.assertTrue(stop_called.wait(2))
            # interrupt() cannot finish while the admission callback holds the
            # shared cancellation lock.
            self.assertFalse(interrupt_finished.wait(0.2))
            release.set()
            runner.join(5)
            stopper.join(5)
            self.assertFalse(runner.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(holder["reply"], "Interrupted by the user.")
            self.assertEqual(backend.messages, [])
            session.close()

    def test_policy_only_tool_block_is_not_reported_as_pending_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "write_file",
                                                 "arguments": {"path": "blocked.txt", "text": "no"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "was blocked", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend, approve=lambda name, args: True)
            session.config.tools.approval = "deny_risky"
            self.assertEqual(session.run_turn("write it"), "was blocked")
            self.assertFalse((Path(config.workspace) / "blocked.txt").exists())
            echo = session.log.get(2)
            self.assertTrue(echo.meta["blocked"])
            tool_message = backend.messages[-1][-1]["content"]
            self.assertIn("deny_risky policy", tool_message)
            self.assertIn("No action ran", tool_message)
            self.assertIn("no approval is pending", tool_message)
            self.assertNotIn("Approval required", tool_message)
            turn_state = backend.messages[0][3]["content"]
            self.assertIn("Goblin tool policy: deny_risky", turn_state)
            self.assertIn("A tool reported as Blocked did not run", turn_state)
            self.assertNotIn("Requested approval is pending", turn_state)
            session.close()

    def test_missing_approver_tool_block_is_not_reported_as_pending_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {"content": "", "tool_calls": [{"id": "c1", "name": "write_file",
                                                 "arguments": {"path": "blocked.txt", "text": "no"}}],
                 "usage": {}, "finish_reason": "tool_calls"},
                {"content": "was blocked", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(temp, backend)
            self.assertEqual(session.run_turn("write it"), "was blocked")
            self.assertFalse((Path(config.workspace) / "blocked.txt").exists())
            echo = session.log.get(2)
            self.assertTrue(echo.meta["blocked"])
            tool_message = backend.messages[-1][-1]["content"]
            self.assertIn("no approval path", tool_message)
            self.assertIn("No action ran", tool_message)
            self.assertNotIn("operator denied", tool_message)
            self.assertNotIn("Approval required", tool_message)
            session.close()

    def test_interrupt_stops_a_running_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = BlockingBackend()
            session, _config = make_session(temp, backend, approve=lambda name, args: True)
            holder = {}
            thread = threading.Thread(target=lambda: holder.setdefault("reply", session.run_turn("wait")))
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            self.assertTrue(session.interrupt())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["reply"], "Interrupted by the user.")
            kinds = [message.kind for message in session.log.iter()]
            self.assertEqual(kinds, ["user", "agent"])
            self.assertTrue(session.log.get(1).meta.get("interrupted"))
            session.close()


    def test_model_wait_emits_live_heartbeats(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = BlockingBackend()
            events = []
            session, _config = make_session(temp, backend,
                                            delta_sink=lambda kind, text: events.append((kind, text)))
            session.heartbeat_interval = 0.02
            thread = threading.Thread(target=lambda: session.run_turn("wait"))
            thread.start()
            self.assertTrue(backend.entered.wait(3))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if any(kind == "heartbeat" for kind, _text in events):
                    break
                time.sleep(0.01)
            backend.cancel()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            heartbeats = [text for kind, text in events if kind == "heartbeat"]
            self.assertTrue(heartbeats, "No live heartbeat during the model wait")
            self.assertIn("Waiting on the model", heartbeats[0])
            session.close()


if __name__ == "__main__":
    unittest.main()

"""Prompt budget: baseline index, schema copy, ordering, envelope, prune, admission.

The mechanisms port the upstream harness prompt work (2026-11-04): a fresh turn
carried 82,548 bytes here, 54,718 of them inlined governing-baseline bodies and
12,552 of tool JSON already present in the API tools parameter.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Tests" / "Python"))
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.backend import BackendError
from test_session import FakeBackend, durable_rows, make_session


class CapturingBackend(FakeBackend):
    def __init__(self, responses):
        super().__init__(responses)
        self.tool_schemas = []

    def chat(self, messages, tools=None, **options):
        self.tool_schemas.append(tools)
        return super().chat(messages, tools=tools, **options)


def request_bytes(messages, schema):
    """Bytes of one model request: every message plus the API tools schema."""
    total = 0
    for message in messages:
        total += len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    total += len(json.dumps(schema or [], ensure_ascii=False).encode("utf-8"))
    return total


class PromptBudgetTests(unittest.TestCase):
    def test_fresh_turn_stays_under_the_model_surface_tripwire(self):
        # 45,000 sits just above the measured 38,838-byte fresh turn. Raising
        # this constant is a deliberate change with a stated reason.
        limit = 45000
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn("Say one short line.")
                size = request_bytes(backend.messages[0], backend.tool_schemas[0])
                self.assertLess(size, limit)
            finally:
                session.close()

    def test_default_baseline_is_indexed_and_loadable(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn("Inspect the available working procedures.")
                prefix = backend.messages[0][0]["content"]
                self.assertIn("pstack-poteto-mode", prefix)
                self.assertIn("their bodies are", prefix)
                core = session.shelf.find("pstack-poteto-mode")["body"]
                self.assertNotIn(core, prefix)
                loaded = session.tools.execute("skill_load", {"name": "pstack-poteto-mode"})
                self.assertIn("skill_read pstack-poteto-mode playbooks/<title>.md", loaded)
            finally:
                session.close()

    def test_inline_mode_keeps_full_bodies(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend,
                                      advanced={"skills": {"baseline_mode": "inline"}})
            try:
                session.run_turn("Inspect the available working procedures.")
                prefix = backend.messages[0][0]["content"]
                core = session.shelf.find("pstack-poteto-mode")["body"]
                self.assertIn(core, prefix)
                self.assertIn("already loaded and active", prefix)
            finally:
                session.close()

    def test_tools_block_is_compact_and_gist_follows_it(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn("Say one short line.")
                first = backend.messages[0]
                schema = backend.tool_schemas[0]
                tools_block = first[1]["content"]
                self.assertTrue(tools_block.startswith("<tools>"))
                self.assertNotIn('"parameters"', tools_block)
                self.assertEqual(len(tools_block.splitlines()) - 2, len(schema))
                self.assertTrue(first[2]["content"].startswith("<chat>"))
                self.assertIn('"parameters"', json.dumps(schema))
            finally:
                session.close()

    def test_scoped_exposure_defers_and_all_restores_flat(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn("Say one short line.")
                names = [item["function"]["name"] for item in backend.tool_schemas[0]]
                self.assertIn("tool_use", names)
                self.assertNotIn("skill_write", names)
                self.assertIn("skill_write", session.tools.names())
            finally:
                session.close()
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([])
            session, _ = make_session(temp, backend,
                                      advanced={"tools": {"exposure": "all"}})
            try:
                session.run_turn("Say one short line.")
                names = [item["function"]["name"] for item in backend.tool_schemas[0]]
                self.assertNotIn("tool_use", names)
                self.assertIn("skill_write", names)
            finally:
                session.close()

    def test_envelope_describe_direct_call_and_unknown_name(self):
        skill_text = ("---\nname: from-envelope\n"
                      "description: Written through the envelope.\n"
                      "---\nBody")
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([
                {"content": "", "tool_calls": [{"id": "d", "name": "tool_use",
                                                "arguments": {"name": "skill_write", "describe": True}}]},
                {"content": "", "tool_calls": [{"id": "w", "name": "tool_use",
                                                "arguments": {"name": "skill_write",
                                                              "arguments": {"name": "from-envelope",
                                                                            "text": skill_text}}}]},
                {"content": "", "tool_calls": [{"id": "u", "name": "tool_use",
                                                "arguments": {"name": "nope"}}]},
                {"content": "done", "tool_calls": []},
            ])
            session, _ = make_session(temp, backend, approve=lambda name, args: True)
            try:
                self.assertEqual(session.run_turn("Use the deferred tool."), "done")
                written = session.shelf.user_root / "from-envelope" / "SKILL.md"
                self.assertTrue(written.exists())
                contents = [message.get("content") for message in backend.messages[1]
                            if message.get("role") == "tool"]
                self.assertIn("skill_write", contents[0])
                self.assertIn("Create or replace a skill", contents[0])
                self.assertIn("from-envelope", contents[1])
                self.assertIn("Unknown tool: nope", contents[2])
                logged = [row for row in durable_rows(session)
                          if row["kind"] == "echo" and row["meta"].get("name") == "skill_write"]
                self.assertEqual(len(logged), 1)
            finally:
                session.close()

    def test_older_tool_results_are_pruned_and_the_log_keeps_more(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([
                {"content": "", "tool_calls": [{"id": "a", "name": "big_probe", "arguments": {}}]},
                {"content": "", "tool_calls": [{"id": "b", "name": "big_probe", "arguments": {}}]},
                {"content": "done", "tool_calls": []},
            ])
            session, _ = make_session(temp, backend)

            def big_probe():
                return "X" * 40000

            session.tools.register("big_probe", "Return large text.", {"type": "object"}, big_probe)
            try:
                self.assertEqual(session.run_turn("Run the probe twice."), "done")
                second = backend.messages[1]
                tool_contents = [m["content"] for m in second if m.get("role") == "tool"]
                self.assertEqual(len(tool_contents), 2)
                older, newest = tool_contents
                self.assertIn("[clipped]", older)
                self.assertLess(len(older), 9000)
                # The newest result keeps the tool's own clip_chars copy; only
                # the history prune shrinks the older ones to 8,192.
                self.assertGreater(len(newest), 20000)
                self.assertGreater(len(newest), len(older) * 2)
                echoes = [row for row in durable_rows(session)
                          if row["kind"] == "echo" and row["meta"].get("name") == "big_probe"]
                self.assertTrue(all(len(row["text"]) > len(older) for row in echoes))
                third = [m["content"] for m in backend.messages[2] if m.get("role") == "tool"]
                self.assertTrue(all("[clipped]" in text for text in third))
            finally:
                session.close()

    def test_over_budget_turn_refuses_before_sending(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([{"content": "done", "tool_calls": []}])
            session, _ = make_session(temp, backend,
                                      advanced={"prompt": {"max_bytes": 1000}})
            try:
                with self.assertRaises(BackendError) as caught:
                    session.run_turn("Say one short line.")
                self.assertEqual(caught.exception.kind, "prompt_budget")
                self.assertEqual(backend.messages, [])
                notes = [row for row in durable_rows(session)
                         if row["kind"] == "note" and row["meta"].get("prompt_budget")]
                self.assertEqual(len(notes), 1)
            finally:
                session.close()

    def test_max_bytes_zero_disables_admission(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([{"content": "done", "tool_calls": []}])
            session, _ = make_session(temp, backend,
                                      advanced={"prompt": {"max_bytes": 0}})
            try:
                self.assertEqual(session.run_turn("Say one short line."), "done")
                self.assertEqual(len(backend.messages), 1)
            finally:
                session.close()

    def test_budget_note_names_components_when_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([{"content": "done", "tool_calls": []}])
            session, _ = make_session(temp, backend,
                                      advanced={"prompt": {"budget_note_bytes": 1000}})
            try:
                session.run_turn("Say one short line.")
                notes = [row for row in durable_rows(session)
                         if row["kind"] == "note" and row["meta"].get("prompt_budget")]
                self.assertEqual(len(notes), 1)
                budget = notes[0]["meta"]["prompt_budget"]
                for component in ("system", "tools_block", "gist", "state", "message",
                                  "tools_schema", "total", "estimated_tokens"):
                    self.assertIn(component, budget)
            finally:
                session.close()

    def test_usage_anchor_follows_the_provider_count(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = CapturingBackend([
                {"content": "first", "tool_calls": [], "usage": {"prompt_tokens": 9700}},
                {"content": "second", "tool_calls": []},
            ])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn("Say one line.")
                session.run_turn("Say another line.")
                budget = session.last_prompt_budget
                self.assertEqual(budget["anchor_prompt_tokens"], 9700)
                self.assertGreater(budget["estimated_tokens"], 0)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()

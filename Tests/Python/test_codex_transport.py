"""Codex account transport and account credential tests against a local fake provider."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.backend import SIGN_IN_REQUIRED, Backend, BackendError
from goblin_core.credentials import ResolvedCredential
from goblin_core.providers import CodexResponsesAdapter


class ProviderServer(HTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.replies = []
        self.last_payload = {}
        self.last_headers = {}
        self.headers_seen = []
        self.request_count = 0
        self.status = 200
        self.content_type = "text/event-stream"


def completed(text):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"total_tokens": 3},
        },
    }


def tool_call(call_id="call_1", name="probe", arguments='{"x": 1}'):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "function_call", "id": "fc_response_item", "call_id": call_id,
                        "name": name, "arguments": arguments}],
            "usage": {},
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert isinstance(self.server, ProviderServer)
        length = int(self.headers.get("Content-Length", "0"))
        self.server.last_payload = json.loads(self.rfile.read(length))
        self.server.last_headers = {name.lower(): value for name, value in self.headers.items()}
        self.server.headers_seen.append(self.server.last_headers)
        self.server.request_count += 1
        if self.server.status != 200:
            body = b'{"error": {"message": "provider body"}}'
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        reply = self.server.replies.pop(0) if self.server.replies else [completed("codex reply")]
        self.send_response(200)
        if self.server.content_type:
            self.send_header("Content-Type", self.server.content_type)
        self.end_headers()
        for event in reply:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def log_message(self, *args):
        return


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ProviderServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def backend(self, **options):
        options.setdefault("credential_provider",
                           lambda: ResolvedCredential("token-1", "oauth", {"ChatGPT-Account-ID": "acct-1"}))
        options.setdefault("retries", 0)
        return Backend(self.base, "gpt-5-codex", "codex_responses", **options)

    def test_system_prompt_moves_into_instructions(self):
        backend = self.backend()
        result = backend.chat([
            {"role": "system", "content": "Be exact."},
            {"role": "developer", "content": "Use the workspace."},
            {"role": "user", "content": "hi"},
        ], tools=None)
        self.assertEqual(result["content"], "codex reply")
        payload = self.server.last_payload
        self.assertEqual(payload["instructions"], "Be exact.\n\nUse the workspace.")
        self.assertEqual(payload["input"], [{"role": "user", "content": "hi"}])
        for item in payload["input"]:
            self.assertNotEqual(item.get("role"), "system")

    def test_request_keeps_tools_and_omits_local_model_fields(self):
        backend = self.backend()
        tools = [{"type": "function", "function": {
            "name": "probe", "description": "Probe the workspace",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}]
        backend.chat([{"role": "system", "content": "Be exact."},
                      {"role": "user", "content": "hi"}], tools=tools,
                     reasoning_effort="high", max_tokens=100, thinking_budget=50,
                     chat_template_kwargs={"enable_thinking": True})
        payload = self.server.last_payload
        self.assertEqual(payload["store"], False)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["include"], ["reasoning.encrypted_content"])
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertTrue(payload["parallel_tool_calls"])
        self.assertEqual(payload["tools"], [{
            "type": "function", "name": "probe", "description": "Probe the workspace",
            "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
            "strict": False}])
        for name in ("max_tokens", "max_output_tokens", "thinking_budget", "chat_template_kwargs"):
            self.assertNotIn(name, payload)

    def test_streaming_is_forced_for_the_codex_endpoint(self):
        backend = self.backend(stream=False)
        result = backend.chat([{"role": "system", "content": "Be exact."},
                               {"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(result["content"], "codex reply")
        self.assertTrue(self.server.last_payload["stream"])

    def test_mandatory_stream_is_parsed_without_content_type(self):
        self.server.content_type = ""
        backend = self.backend(stream=False)
        result = backend.chat([
            {"role": "system", "content": "Be exact."},
            {"role": "user", "content": "hi"},
        ], tools=[])
        self.assertEqual(result["content"], "codex reply")
        self.assertEqual(self.server.last_headers["accept"], "text/event-stream")

    def test_saved_reasoning_reaches_the_provider(self):
        backend = self.backend(parameters={"reasoning_effort": "low"})
        backend.chat([{"role": "user", "content": "hi"}], tools=[])
        self.assertEqual(self.server.last_payload["reasoning"], {"effort": "low"})
        backend.chat([{"role": "user", "content": "hi"}], tools=[], reasoning_effort="high")
        self.assertEqual(self.server.last_payload["reasoning"], {"effort": "high"})
        self.assertEqual(backend.parameters["reasoning_effort"], "low")

    def test_account_headers_and_token_reach_the_request(self):
        backend = self.backend()
        backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(self.server.last_headers.get("authorization"), "Bearer token-1")
        self.assertEqual(self.server.last_headers.get("chatgpt-account-id"), "acct-1")
        self.assertEqual(backend.last_auth, "oauth")

    def test_missing_account_credential_is_a_sign_in_error_without_a_key_fallback(self):
        key_calls = []

        def broken():
            raise RuntimeError("secret sk-live-value")

        backend = Backend(self.base, "gpt-5-codex", "codex_responses",
                          credential_provider=broken,
                          key_provider=lambda: key_calls.append(True) or "key",
                          retries=0)
        with self.assertRaises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertEqual(str(caught.exception), SIGN_IN_REQUIRED)
        self.assertNotIn("sk-live-value", str(caught.exception))
        self.assertEqual(key_calls, [])
        self.assertEqual(self.server.request_count, 0)

    def test_credential_is_resolved_for_each_request(self):
        tokens = ["token-1", "token-2"]

        def credential():
            return ResolvedCredential(tokens.pop(0), "oauth")

        backend = Backend(self.base, "gpt-5-codex", "codex_responses",
                          credential_provider=credential, retries=0)
        backend.chat([{"role": "user", "content": "one"}], tools=None)
        backend.chat([{"role": "user", "content": "two"}], tools=None)
        self.assertEqual(self.server.request_count, 2)
        seen = [headers["authorization"] for headers in self.server.headers_seen]
        self.assertEqual(seen, ["Bearer token-1", "Bearer token-2"])

    def test_empty_account_credential_is_a_sign_in_error(self):
        backend = Backend(self.base, "gpt-5-codex", "codex_responses",
                          credential_provider=lambda: ResolvedCredential("", "oauth"),
                          retries=0)
        with self.assertRaises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertEqual(self.server.request_count, 0)

    def test_rejected_account_grant_reports_the_sign_in_path(self):
        self.server.status = 401
        backend = self.backend()
        with self.assertRaises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(caught.exception.category, "authentication")
        self.assertEqual(str(caught.exception), SIGN_IN_REQUIRED)
        self.assertNotIn("provider body", caught.exception.detail)

        key_backend = Backend(self.base, "gpt-5-codex", "codex_responses",
                              key_provider=lambda: "key-1", retries=0)
        with self.assertRaises(BackendError) as key_caught:
            key_backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(str(key_caught.exception), "Backend: HTTP 401")

    def test_streamed_text_redacts_the_account_token(self):
        self.server.replies.append([{
            "type": "response.output_text.delta", "delta": "leaked token-1 here"},
            completed("leaked token-1 here")])
        deltas = []
        backend = self.backend()
        backend.on_delta = lambda kind, text: deltas.append(text)
        backend.chat([{"role": "user", "content": "hi"}], tools=None)
        joined = "".join(deltas)
        self.assertNotIn("token-1", joined)
        self.assertIn("[REDACTED]", joined)

    def test_tool_replay_drops_response_bound_item_ids(self):
        self.server.replies.append([tool_call()])
        backend = self.backend()
        first = backend.chat([{"role": "user", "content": "use the tool"}], tools=None)
        self.assertEqual(first["tool_calls"][0]["id"], "call_1")
        backend.chat([
            {"role": "user", "content": "use the tool"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "probe", "arguments": '{"x": 1}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ], tools=None)
        items = self.server.last_payload["input"]
        calls = [item for item in items if item.get("type") == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("id", calls[0])
        self.assertEqual(calls[0]["call_id"], "call_1")
        self.assertEqual(calls[0]["arguments"], '{"x": 1}')
        outputs = [item for item in items if item.get("type") == "function_call_output"]
        self.assertEqual(outputs, [{"type": "function_call_output", "call_id": "call_1", "output": "ok"}])
        self.assertNotIn("previous_response_id", self.server.last_payload)

    def test_tool_replay_keeps_encrypted_reasoning_items_without_ids(self):
        reasoning = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "rs_response_item",
                     "encrypted_content": "sealed-reasoning", "summary": []},
                    {"type": "function_call", "id": "fc_response_item", "call_id": "call_1",
                     "name": "probe", "arguments": '{"x": 1}'},
                ],
                "usage": {},
            },
        }
        self.server.replies.append([reasoning])
        backend = self.backend()
        backend.chat([{"role": "user", "content": "use the tool"}], tools=None)
        backend.chat([
            {"role": "user", "content": "use the tool"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "probe", "arguments": '{"x": 1}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ], tools=None)
        items = self.server.last_payload["input"]
        reasoning_items = [item for item in items if item.get("type") == "reasoning"]
        self.assertEqual(len(reasoning_items), 1)
        self.assertEqual(reasoning_items[0]["encrypted_content"], "sealed-reasoning")
        self.assertNotIn("id", reasoning_items[0])
        calls = [item for item in items if item.get("type") == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("id", calls[0])

    def test_failed_response_event_is_a_failed_request(self):
        self.server.replies.append([{"type": "response.failed",
                                     "response": {"status": "failed"}}])
        backend = self.backend()
        with self.assertRaises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(caught.exception.kind, "parse")
        self.assertIn("failed response", caught.exception.detail)

    def test_incomplete_and_refusal_keep_their_finish_reasons(self):
        adapter = CodexResponsesAdapter()
        incomplete = adapter.decode({
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut"}]}],
        })
        self.assertEqual(incomplete["finish_reason"], "length")
        refused = adapter.decode({
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
        })
        self.assertEqual(refused["finish_reason"], "content_filter")
        self.assertEqual(refused["content"], "no")

    def test_server_side_continuation_is_rejected(self):
        adapter = CodexResponsesAdapter()
        with self.assertRaises(ValueError):
            adapter.encode("gpt-5-codex", [], None, {"previous_response_id": "resp_1"})


if __name__ == "__main__":
    unittest.main()

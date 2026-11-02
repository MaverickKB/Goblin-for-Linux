"""HTTP model transport tests against a local fake provider."""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.backend import Backend, BackendError


class ProviderServer(HTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.behavior = 'json'
        self.last_payload = {}
        self.request_count = 0


class Handler(BaseHTTPRequestHandler):
    mode = "chat"

    def do_POST(self):
        assert isinstance(self.server, ProviderServer)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.last_payload = payload
        self.server.request_count += 1
        behavior = getattr(self.server, 'behavior', 'json')
        if behavior != 'json':
            self.stream_response(behavior)
            return
        if self.path == "/chat/completions":
            body = {"choices": [{"message": {"content": "chat reply"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 4}}
        elif self.path == "/responses":
            body = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "responses reply"}]}], "usage": {}}
        else:
            body = {"error": "unknown"}
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def stream_response(self, behavior):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if behavior == 'heartbeat':
            for _ in range(24):
                self.wfile.write(b': router waiting for serving endpoint\n\n')
                self.wfile.flush()
                time.sleep(0.05)
        chunk = {'choices': [{'delta': {'content': 'streamed reply'}, 'finish_reason': None}]}
        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        if behavior != 'truncated':
            self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

    def log_message(self, *args):
        return


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.server = ProviderServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_chat_completions(self):
        backend = Backend(self.base, "test-model", "chat_completions",
                          key_provider=lambda: "test-key")
        result = backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(result["content"], "chat reply")
        self.assertEqual(backend.last_auth, "api_key")

    def test_responses(self):
        backend = Backend(self.base, "test-model", "responses", key_provider=lambda: "test-key")
        result = backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(result["content"], "responses reply")

    def test_background_call_streams_without_a_display_callback(self):
        self.server.behavior = 'stream'
        backend = Backend(self.base, 'test-model', 'chat_completions', retries=0)
        self.assertIsNone(backend.on_delta)
        result = backend.chat([{'role': 'user', 'content': 'Summarize the source'}], tools=[])
        self.assertTrue(self.server.last_payload['stream'])
        self.assertEqual(result['content'], 'streamed reply')

    def test_background_stream_keeps_one_request_through_router_queue_heartbeats(self):
        self.server.behavior = 'heartbeat'
        backend = Backend(self.base, 'test-model', 'chat_completions', timeout=1, retries=0)
        started = time.monotonic()
        result = backend.chat([{'role': 'user', 'content': 'Summarize the source'}], tools=[])
        self.assertGreater(time.monotonic() - started, backend.timeout)
        self.assertEqual(result['content'], 'streamed reply')
        self.assertEqual(self.server.request_count, 1)

    def test_incomplete_stream_is_a_failed_request_not_a_completed_summary(self):
        self.server.behavior = 'truncated'
        backend = Backend(self.base, 'test-model', 'chat_completions', retries=0)
        with self.assertRaises(BackendError) as caught:
            backend.chat([{'role': 'user', 'content': 'Summarize the source'}], tools=[])
        self.assertEqual(caught.exception.kind, 'parse')
        self.assertIn('ended before completion', caught.exception.detail)

    def test_explicit_non_streaming_setting_remains_supported(self):
        backend = Backend(self.base, 'test-model', 'chat_completions', stream=False)
        result = backend.chat([{'role': 'user', 'content': 'Hello'}], tools=[])
        self.assertFalse(self.server.last_payload['stream'])
        self.assertEqual(result['content'], 'chat reply')

    def test_missing_model_is_a_configuration_error(self):
        backend = Backend("", "", "chat_completions", key_provider=lambda: "test-key")
        with self.assertRaises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "hi"}], tools=None)
        self.assertEqual(caught.exception.category, "configuration")

    def test_keychain_failure_is_reported_without_the_value(self):
        def broken():
            raise RuntimeError("not installed")
        backend = Backend(self.base, "test-model", "chat_completions", key_provider=broken)
        with self.assertRaises(BackendError):
            backend.chat([{"role": "user", "content": "hi"}], tools=None)


if __name__ == "__main__":
    unittest.main()

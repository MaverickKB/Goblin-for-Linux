"""Provider errors and user input never persist a resolved model credential."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.backend import Backend, BackendError, _redact_secret
from goblin_core.host import GoblinHost

KEY = "synthetic-model-redaction-probe"


class EchoCredentialError(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"error": "Invalid request for " + self.headers.get("Authorization", "")}).encode()
        self.send_response(400)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class CredentialLogTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), EchoCredentialError)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_provider_error_redacts_the_sent_header(self):
        backend = Backend(self.endpoint, "test", "chat_completions", key_provider=lambda: KEY)
        with self.assertRaises(BackendError) as failure:
            backend.chat([{"role": "user", "content": "hello"}])
        details = json.dumps(failure.exception.diagnostics())
        self.assertNotIn(KEY, details)
        self.assertIn("[REDACTED]", details)

    def test_structured_provider_data_redacts_keys_and_values(self):
        data = {KEY: {"value": [KEY, "safe"]}}
        cleaned = _redact_secret(data, KEY)
        self.assertNotIn(KEY, json.dumps(cleaned))
        self.assertEqual(cleaned["[REDACTED]"]["value"], ["[REDACTED]", "safe"])

    def test_pasted_key_and_provider_error_are_redacted_in_durable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            host = GoblinHost(Path(directory), ROOT / "product.json")
            try:
                settings = host.config.settings_dict()
                settings["model_base_url"] = self.endpoint
                settings["model"] = "test"
                host.save_settings(settings)
                class Provider:
                    def model_key(self, endpoint=None):
                        return KEY
                host.secret_provider = Provider()
                turn = host.submit(host.slug, "Check " + KEY)
                deadline = time.monotonic() + 5
                state = host.turn(host.slug, turn)
                while time.monotonic() < deadline:
                    state = host.turn(host.slug, turn)
                    if state["state"] not in ("queued", "running"):
                        break
                    time.sleep(0.01)
                self.assertEqual(state["state"], "error")
                paths = list((Path(directory) / "chat" / host.slug / "log").glob("*.jsonl"))
                self.assertTrue(paths)
                for path in paths:
                    text = path.read_text()
                    self.assertNotIn(KEY, text)
                    self.assertIn("[REDACTED]", text)
            finally:
                host.shutdown()

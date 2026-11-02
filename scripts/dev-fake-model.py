#!/usr/bin/env python3
"""A minimal OpenAI-compatible model for local Goblin smoke tests.

Run it, point Goblin's base URL at http://127.0.0.1:18080/v1, and send a
message. It answers without a network call or a key.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18080


def answer_for(payload):
    messages = payload.get("messages") or []
    last = ""
    for message in messages:
        if message.get("role") == "user":
            last = message.get("content") or ""
    return "Linux model online. You said: " + last


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({"data": [{"id": "linux-smoke", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        text = answer_for(payload)
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in text.split(" "):
                chunk = {"choices": [{"delta": {"content": word + " "}, "index": 0}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            done = {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}
            self.wfile.write(("data: " + json.dumps(done) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps({
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("fake model on http://127.0.0.1:" + str(PORT) + "/v1")
    server.serve_forever()

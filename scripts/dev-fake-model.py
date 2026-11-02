#!/usr/bin/env python3
"""A minimal OpenAI-compatible model for local Goblin smoke tests.

Run it, point Goblin's base URL at http://127.0.0.1:18080/v1, and send a
message. It answers without a network call or a key.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18080


def last_user_text(payload):
    text = ""
    for message in payload.get("messages") or []:
        if message.get("role") == "user":
            text = message.get("content") or ""
    return text


def wants_shell(payload):
    messages = payload.get("messages") or []
    has_tool_result = any(message.get("role") == "tool" for message in messages)
    return last_user_text(payload).lower().startswith("run shell") and not has_tool_result


def shell_call_events():
    function = {"name": "shell", "arguments": json.dumps({"command": "echo approved"})}
    call = {"index": 0, "id": "call-1", "type": "function", "function": function}
    chunk = {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls", "index": 0}]}
    return [
        "data: " + json.dumps(chunk) + "\n\n",
        "data: [DONE]\n\n",
    ]


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
        print("POST roles=" + json.dumps([
            [message.get("role"), str(message.get("content"))[:120]]
            for message in payload.get("messages") or []
        ]), flush=True)
        if wants_shell(payload) and payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in shell_call_events():
                self.wfile.write(event.encode())
                self.wfile.flush()
            return
        if wants_shell(payload):
            body = json.dumps({
                "choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "shell",
                                 "arguments": json.dumps({"command": "echo approved"})}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        text = "Linux model online. You said: " + last_user_text(payload)
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

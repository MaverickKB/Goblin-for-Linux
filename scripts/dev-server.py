#!/usr/bin/env python3
"""Static file server for Tauri development, with caching disabled."""

import http.server
import os
import sys

PORT = 8099


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, *args):
        return


if __name__ == "__main__":
    directory = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    handler = lambda *args, **kwargs: Handler(*args, directory=directory, **kwargs)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    print("Goblin dev UI on http://127.0.0.1:" + str(PORT))
    server.serve_forever()

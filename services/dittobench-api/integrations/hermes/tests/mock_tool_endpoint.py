"""Observed-tool endpoint used by the live adapter integration smoke test."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        print(json.dumps(request, sort_keys=True), flush=True)
        body = json.dumps({"result": "0.2379, 0.0236, 0.7385"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 21436
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

"""Tiny OpenAI-compatible server for the container integration smoke test."""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        has_result = any(message.get("role") == "tool" for message in request.get("messages", []))
        if has_result:
            message = {"role": "assistant", "content": "Vellichor-47."}
            finish_reason = "stop"
        else:
            names = {
                tool.get("function", {}).get("name")
                for tool in request.get("tools", [])
            }
            native = "session_search" in names
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_memory_1",
                        "type": "function",
                        "function": {
                            "name": "session_search" if native else "search_memories",
                            "arguments": json.dumps(
                                {"query": "Vellichor"} if native else {"queries": ["Vellichor"]}
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"

        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = dict(message)
            delta.pop("role", None)
            chunks = [
                {
                    "id": "chatcmpl-smoke",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": request.get("model", "test"),
                    "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}],
                },
                {
                    "id": "chatcmpl-smoke",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": request.get("model", "test"),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return

        response = {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", "test"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

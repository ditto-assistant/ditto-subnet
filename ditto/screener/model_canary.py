"""Ephemeral fake model gateway used by the screener's runtime canary.

The server runs outside the untrusted harness container and implements the small
OpenAI-compatible surface a harness needs for one ``/run`` call. Its response
contains a random token that never appears in the harness request. A passing
harness must call this server and return that token, proving it consumed a model
response rather than merely issuing a throwaway request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from types import TracebackType

_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 1024 * 1024
_EMBED_DIMENSIONS = 32
LOCKED_HARNESS_MODEL = "qwen/qwen3-32b"


class ModelCallCanary:
    """Short-lived OpenAI-compatible HTTP server with observable call state."""

    def __init__(self) -> None:
        self.token = f"ditto-canary-{secrets.token_hex(16)}"
        self.model_calls = 0
        self._server: asyncio.Server | None = None

    @property
    def gateway_url(self) -> str:
        """URL the Docker container can use for this host-side server."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("model canary server is not running")
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://host.docker.internal:{port}"

    async def __aenter__(self) -> ModelCallCanary:
        # Bind all host interfaces because a bridge-network container reaches the
        # host through Docker's gateway, not through host loopback.
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", 0)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = "200 OK"
        payload: dict[str, object]
        try:
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=5
            )
            if len(raw_headers) > _MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            lines = raw_headers.decode("latin-1").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers = {
                key.strip().casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            length = int(headers.get("content-length", "0"))
            if length < 0 or length > _MAX_BODY_BYTES:
                raise ValueError("body too large")
            if length:
                await asyncio.wait_for(reader.readexactly(length), timeout=5)

            if method == "POST" and path.rstrip("/") in {
                "/v1/chat/completions",
                "/chat/completions",
            }:
                self.model_calls += 1
                payload = {
                    "id": "chatcmpl-ditto-screening-canary",
                    "object": "chat.completion",
                    "created": 0,
                    "model": LOCKED_HARNESS_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": self.token},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            elif method == "POST" and path.rstrip("/") in {
                "/v1/responses",
                "/responses",
            }:
                self.model_calls += 1
                payload = {
                    "id": "resp_ditto_screening_canary",
                    "object": "response",
                    "status": "completed",
                    "model": LOCKED_HARNESS_MODEL,
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": self.token}],
                        }
                    ],
                    "output_text": self.token,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            elif method == "POST" and path.rstrip("/") in {
                "/api/embed",
                "/api/embeddings",
                "/v1/embeddings",
            }:
                vector = [0.0] * _EMBED_DIMENSIONS
                vector[0] = 1.0
                payload = {
                    "model": LOCKED_HARNESS_MODEL,
                    "embeddings": [vector],
                    "data": [{"index": 0, "embedding": vector}],
                }
            else:
                status = "404 Not Found"
                payload = {"error": {"message": "unsupported canary endpoint"}}
        except (
            TimeoutError,
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            status = "400 Bad Request"
            payload = {"error": {"message": "malformed canary request"}}

        body = json.dumps(payload, separators=(",", ":")).encode()
        writer.write(
            f"HTTP/1.1 {status}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

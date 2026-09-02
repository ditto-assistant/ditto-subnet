"""Ephemeral fake model gateway for isolated harness startup and private audits.

The server runs in a locked-down sidecar on the harness's isolated Docker
network and implements the small OpenAI-compatible surface a harness needs for
optional private behavioral challenge. The public v6 build gate never calls
``POST /run`` and never treats this server as proof of causal model use.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import secrets
import ssl
import time
from pathlib import Path
from types import TracebackType

_CHAT_ROUTES = frozenset(
    {
        "/v1/chat/completions",
        "/chat/completions",
        "/api/v1/chat/completions",
    }
)
_RESPONSE_ROUTES = frozenset(
    {
        "/v1/responses",
        "/responses",
        "/api/v1/responses",
    }
)
_EMBED_ROUTES = frozenset(
    {
        "/api/embed",
        "/api/embeddings",
        "/v1/embeddings",
        "/api/v1/embeddings",
    }
)

_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 1024 * 1024
_EMBED_DIMENSIONS = 768
LOCKED_HARNESS_MODEL = "qwen/qwen3-32b"
# Fallback echoed only when a caller omits the ``model`` field. It is a common,
# generic name so the container cannot fingerprint the screener from it.
_DEFAULT_ECHO_MODEL = "gpt-4o-mini"


class FakeModelGateway:
    """Short-lived OpenAI-compatible HTTP server with observable call state.

    Responses are intentionally indistinguishable from a real provider: the
    ``id`` is random, ``created`` is a real timestamp, the requested ``model``
    is echoed back, and the assistant content is a high-entropy opaque token
    rather than any ``ditto``/``fake``/``screening`` marker. A harness cannot
    fingerprint "I am being screened" from anything it can observe here.

    A two-token correctness oracle makes model use checkable without any
    heuristic: the gateway returns ``response_text`` (a per-container nonce) on
    a first turn, and ``oracle_answer`` (a per-container transform) only once a
    request body carries that nonce back. A static lookup table that never
    performs the second gateway round-trip cannot produce ``oracle_answer``.
    """

    def __init__(
        self,
        *,
        response_text: str | None = None,
        oracle_answer: str | None = None,
        host: str = "0.0.0.0",
        port: int = 0,
        state_file: str | None = None,
        latency_range: tuple[float, float] = (0.0, 0.0),
        surface: str = "all",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.response_text = response_text or secrets.token_hex(16)
        self._oracle_answer = oracle_answer
        self.model_calls = 0
        self._host = host
        self._port = port
        self._state_file = state_file
        if surface not in {"all", "model", "embedding"}:
            raise ValueError("surface must be all, model, or embedding")
        self._surface = surface
        low, high = latency_range
        if low < 0 or high < low:
            raise ValueError("latency_range must be a non-negative (low, high) pair")
        self._latency_range = (low, high)
        self._ssl_context = ssl_context
        self._server: asyncio.Server | None = None

    @property
    def gateway_url(self) -> str:
        """URL the Docker container can use for this host-side server."""
        if self._server is None or not self._server.sockets:
            raise RuntimeError("fake model gateway is not running")
        port = int(self._server.sockets[0].getsockname()[1])
        scheme = "https" if self._ssl_context is not None else "http"
        return f"{scheme}://host.docker.internal:{port}"

    async def __aenter__(self) -> FakeModelGateway:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, ssl=self._ssl_context
        )
        return self

    def _record_model_call(self) -> None:
        self.model_calls += 1
        if self._state_file is not None:
            path = Path(self._state_file)
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, b"1\n")
            finally:
                os.close(fd)

    async def _simulate_latency(self) -> None:
        """Sleep a jittered interval so a real model call is not instant.

        A genuine multi-turn harness accumulates this latency across gateway
        round-trips; a static lookup table answers in sub-millisecond time.
        """
        low, high = self._latency_range
        if high > 0:
            await asyncio.sleep(random.uniform(low, high))

    def _response_content(self, body: bytes) -> str:
        """Return the nonce, or the oracle answer once the nonce is fed back.

        The screener knows both tokens, so the correct final answer of a
        nonce round-trip is deterministically checkable without any heuristic.
        """
        if self._oracle_answer is not None and self.response_text in _as_text(body):
            return self._oracle_answer
        return self.response_text

    def _chat_message(self, body: bytes) -> dict[str, object]:
        """Build the assistant message for one chat-completions turn.

        The gateway answers with TEXT content carrying the per-container nonce,
        the same for every caller regardless of whether tools were declared. A
        real provider may answer directly instead of calling a tool, so this is
        indistinguishable from ordinary provider behavior, and it makes the
        model-use check architecture-neutral: any harness that relays the model's
        answer (text-only, one-turn, tool-forwarding, Responses-API) surfaces the
        nonce in a single turn and passes, while a static table that never calls
        the gateway never learns the nonce. A harness that DOES loop and feeds the
        nonce back still works: ``_response_content`` then returns ``oracle_answer``
        (also an accepted gateway token), so the multi-turn path is supported but
        not required.
        """
        return {"role": "assistant", "content": self._response_content(body)}

    @staticmethod
    def _echo_model(body: bytes) -> str:
        """Echo the caller's requested model so the reply carries no screener tell."""
        try:
            parsed = json.loads(body) if body else None
        except (ValueError, UnicodeError):
            parsed = None
        if isinstance(parsed, dict):
            model = parsed.get("model")
            if isinstance(model, str) and model:
                return model
        return _DEFAULT_ECHO_MODEL

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
            path = _route_path(path)
            headers = {
                key.strip().casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            if headers.get("expect", "").casefold() == "100-continue":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            # The gateway only supplies an offline-compatible model surface.
            # Some compatible clients stream chunked bodies differently across
            # architectures, so tolerate body framing quirks here.
            # Drain a well-formed body when possible, but do not turn a body
            # framing quirk into a permanent miner rejection.
            body = b""
            try:
                body = await self._read_request_body(reader, headers)
            except (
                TimeoutError,
                ValueError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as error:
                print(
                    f"fake gateway ignored request-body framing error: {error}",
                    flush=True,
                )

            if (
                self._surface in {"all", "model"}
                and method == "POST"
                and path in _CHAT_ROUTES
            ):
                self._record_model_call()
                await self._simulate_latency()
                message = self._chat_message(body)
                payload = {
                    "id": f"chatcmpl-{secrets.token_hex(12)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": self._echo_model(body),
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls"
                            if "tool_calls" in message
                            else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            elif (
                self._surface == "all" and method == "POST" and path in _RESPONSE_ROUTES
            ):
                self._record_model_call()
                await self._simulate_latency()
                content = self._response_content(body)
                payload = {
                    "id": f"resp_{secrets.token_hex(24)}",
                    "object": "response",
                    "status": "completed",
                    "created_at": int(time.time()),
                    "model": self._echo_model(body),
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    ],
                    "output_text": content,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            elif (
                self._surface in {"all", "model"}
                and method == "GET"
                and path == "/health"
            ):
                payload = {"status": "ok"}
            elif (
                self._surface in {"all", "model"}
                and method == "POST"
                and path == "/tool"
            ):
                # Mock tool-execution sink for the behavioral oracle's
                # tool-shaped RunRequest. It lets the harness's agent loop
                # EXECUTE the tool call the model returned (nonce in its args)
                # and proceed to the second model turn that unlocks the oracle
                # answer. Deliberately NOT a model call: it must not increment
                # the gateway call count (that counts only model round-trips).
                # The result content is irrelevant to the nonce round-trip (the
                # nonce rides the assistant tool_calls message in the
                # transcript), so a benign acknowledgement suffices.
                payload = {"result": "ok", "error": ""}
            elif (
                self._surface in {"all", "embedding"}
                and method == "POST"
                and path in _EMBED_ROUTES
            ):
                vector = [0.0] * _EMBED_DIMENSIONS
                vector[0] = 1.0
                payload = {
                    "model": self._echo_model(body),
                    "embeddings": [vector],
                    "data": [{"index": 0, "embedding": vector}],
                }
            else:
                status = "404 Not Found"
                payload = {
                    "error": {
                        "message": "Unrecognized request URL.",
                        "type": "invalid_request_error",
                        "code": "unknown_url",
                    }
                }
        except (
            TimeoutError,
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            OSError,
        ) as error:
            print(f"fake gateway rejected malformed request: {error}", flush=True)
            status = "400 Bad Request"
            payload = {
                "error": {
                    "message": "We could not parse the JSON body of your request.",
                    "type": "invalid_request_error",
                    "code": "invalid_json",
                }
            }

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

    @staticmethod
    async def _read_request_body(
        reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> bytes:
        """Read a bounded fixed-length or chunked HTTP request body."""
        transfer_encodings = {
            value.strip().casefold()
            for value in headers.get("transfer-encoding", "").split(",")
            if value.strip()
        }
        if transfer_encodings and transfer_encodings != {"chunked"}:
            raise ValueError("unsupported transfer encoding")
        if transfer_encodings:
            return await FakeModelGateway._read_chunked_body(reader)

        length = int(headers.get("content-length", "0"))
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ValueError("body too large")
        if not length:
            return b""
        return await asyncio.wait_for(reader.readexactly(length), timeout=5)

    @staticmethod
    async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            raw_size = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
            size_text = raw_size[:-2].split(b";", 1)[0].strip()
            if not size_text:
                raise ValueError("missing chunk size")
            size = int(size_text, 16)
            if size < 0 or len(body) + size > _MAX_BODY_BYTES:
                raise ValueError("body too large")
            if size == 0:
                while True:
                    trailer = await asyncio.wait_for(
                        reader.readuntil(b"\r\n"), timeout=5
                    )
                    if trailer == b"\r\n":
                        return bytes(body)
            body.extend(await asyncio.wait_for(reader.readexactly(size), timeout=5))
            if await asyncio.wait_for(reader.readexactly(2), timeout=5) != b"\r\n":
                raise ValueError("malformed chunk terminator")


def _route_path(path: str) -> str:
    """Strip query-string and trailing slash for route matching."""
    return path.split("?", 1)[0].rstrip("/") or "/"


def _as_text(body: bytes) -> str:
    """Decode a request body loosely for substring checks; never raises."""
    return body.decode("utf-8", "replace")


def _sidecar_latency_range() -> tuple[float, float]:
    """Realistic model-latency jitter for the container sidecar."""
    raw = os.environ.get("DITTO_FAKE_GATEWAY_LATENCY_RANGE")
    if not raw:
        return (0.2, 0.7)
    try:
        low_text, high_text = raw.split(",", 1)
        low, high = float(low_text), float(high_text)
    except ValueError:
        return (0.2, 0.7)
    if low < 0 or high < low:
        return (0.2, 0.7)
    return (low, high)


def _sidecar_tls_context() -> ssl.SSLContext | None:
    """Load the per-screen OpenRouter leaf when the gate staged TLS files."""
    cert = os.environ.get("DITTO_FAKE_GATEWAY_TLS_CERT")
    key = os.environ.get("DITTO_FAKE_GATEWAY_TLS_KEY")
    if not cert or not key:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


async def _serve_sidecar() -> None:
    """Run production-shaped chat, embedding, and OpenRouter TLS ports."""
    response_text = os.environ["DITTO_FAKE_GATEWAY_RESPONSE"]
    oracle_answer = os.environ.get("DITTO_FAKE_GATEWAY_ORACLE_ANSWER") or None
    state_file = os.environ.get("DITTO_FAKE_GATEWAY_STATE_FILE")
    latency = _sidecar_latency_range()
    chat_gateway = FakeModelGateway(
        response_text=response_text,
        oracle_answer=oracle_answer,
        host="0.0.0.0",
        port=11435,
        state_file=state_file,
        latency_range=latency,
        surface="model",
    )
    embed_gateway = FakeModelGateway(
        response_text=response_text,
        oracle_answer=oracle_answer,
        host="0.0.0.0",
        port=11434,
        state_file=state_file,
        latency_range=latency,
        surface="embedding",
    )
    tls_context = _sidecar_tls_context()
    tls_gateway = (
        FakeModelGateway(
            response_text=response_text,
            oracle_answer=oracle_answer,
            host="0.0.0.0",
            port=443,
            state_file=state_file,
            latency_range=latency,
            surface="all",
            ssl_context=tls_context,
        )
        if tls_context is not None
        else None
    )
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(chat_gateway)
        await stack.enter_async_context(embed_gateway)
        if tls_gateway is not None:
            await stack.enter_async_context(tls_gateway)
        await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover - exercised in Docker E2E
    asyncio.run(_serve_sidecar())

"""Ephemeral fake model gateway for isolated harness startup and private audits.

The server runs in a locked-down sidecar on the harness's isolated Docker
network and implements the small OpenAI-compatible surface a harness needs for
optional private behavioral challenge. The public v6 build gate never calls
``POST /run`` and never treats this server as proof of causal model use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import random
import secrets
import ssl
import time
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qs, urlsplit

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
_SEMANTIC_TOOL_EMISSIONS: dict[str, int] = {}
_SEMANTIC_TOOL_EXECUTED: set[str] = set()


def tool_capability(key: bytes, case_id: str, user_id: str) -> str:
    """Match the scorer's case/user-bound tool capability wire format."""
    case = case_id.encode()
    user = user_id.encode()
    message = (
        b"dittobench-tool-v1\n"
        + str(len(case)).encode()
        + b":"
        + case
        + b"\n"
        + str(len(user)).encode()
        + b":"
        + user
    )
    return (
        base64.urlsafe_b64encode(hmac.digest(key, message, hashlib.sha256))
        .rstrip(b"=")
        .decode()
    )


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
        tool_route: str | None = None,
        tool_key: bytes | None = None,
        semantic_config_file: str | None = None,
        semantic_events_file: str | None = None,
    ) -> None:
        self.response_text = response_text or secrets.token_hex(16)
        self._oracle_answer = oracle_answer
        self.model_calls = 0
        self._host = host
        self._port = port
        self._state_file = state_file
        if surface not in {"all", "model", "embedding", "tool"}:
            raise ValueError("surface must be all, model, embedding, or tool")
        self._surface = surface
        if surface == "tool" and (not tool_route or not tool_key):
            raise ValueError("tool gateway requires a route and capability key")
        self._tool_route = tool_route
        self._tool_key = tool_key
        self._semantic_config_file = semantic_config_file
        self._semantic_events_file = semantic_events_file
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
        probe = self._semantic_probe()
        if probe is not None:
            if probe.get("kind") == "tool":
                result = probe.get("result")
                if isinstance(result, str) and result in _as_text(body):
                    return result
            elif probe.get("kind") == "memory":
                markers = probe.get("markers")
                if isinstance(markers, list):
                    for marker in markers:
                        if isinstance(marker, str) and marker in _as_text(body):
                            return marker
        if self._oracle_answer is not None and self.response_text in _as_text(body):
            return self._oracle_answer
        return self.response_text

    def _semantic_probe(self) -> dict[str, object] | None:
        if not self._semantic_config_file:
            return None
        try:
            raw = Path(self._semantic_config_file).read_bytes()
            if len(raw) > 4096:
                return None
            value = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            return None
        return value if isinstance(value, dict) else None

    def _semantic_event(self, event: str, probe_id: str) -> None:
        if not self._semantic_events_file:
            return
        record = (
            json.dumps(
                {"event": event, "probe_id": probe_id}, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        fd = os.open(
            self._semantic_events_file, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(fd, record)
        finally:
            os.close(fd)

    def _semantic_tool_call(self, body: bytes) -> dict[str, object] | None:
        probe = self._semantic_probe()
        if probe is None or probe.get("kind") != "tool":
            return None
        challenge_token = probe.get("challenge_token")
        if not isinstance(
            challenge_token, str
        ) or challenge_token not in _user_prompt_text(body):
            return None
        result = probe.get("result")
        if isinstance(result, str) and result in _as_text(body):
            return None
        try:
            request = json.loads(body)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(request, dict) or not isinstance(request.get("tools"), list):
            return None
        name = probe.get("name")
        args = probe.get("args")
        probe_id = probe.get("probe_id")
        if (
            not isinstance(name, str)
            or not isinstance(args, dict)
            or not isinstance(probe_id, str)
        ):
            return None
        offered = any(
            isinstance(tool, dict)
            and (
                tool.get("name") == name
                or (
                    isinstance(tool.get("function"), dict)
                    and tool["function"].get("name") == name
                )
            )
            for tool in request["tools"]
        )
        if not offered:
            return None
        self._semantic_event("challenge_seen", probe_id)
        _SEMANTIC_TOOL_EMISSIONS[probe_id] = (
            _SEMANTIC_TOOL_EMISSIONS.get(probe_id, 0) + 1
        )
        self._semantic_event("emitted", probe_id)
        return {
            "id": f"call_{secrets.token_hex(12)}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, separators=(",", ":")),
            },
        }

    def _observe_semantic_context(self, body: bytes) -> None:
        probe = self._semantic_probe()
        if probe is None:
            return
        text = _as_text(body)
        user_prompt = _user_prompt_text(body)
        if probe.get("kind") == "ordinary":
            probe_id = probe.get("probe_id")
            token = probe.get("challenge_token")
            if (
                isinstance(probe_id, str)
                and isinstance(token, str)
                and token in user_prompt
            ):
                self._semantic_event("challenge_seen", probe_id)
        elif probe.get("kind") == "memory":
            challenges = probe.get("challenges")
            if not isinstance(challenges, list):
                return
            for challenge in challenges:
                if not isinstance(challenge, dict):
                    continue
                probe_id = challenge.get("probe_id")
                token = challenge.get("challenge_token")
                forbidden = challenge.get("forbidden")
                if not isinstance(probe_id, str) or not isinstance(token, str):
                    continue
                if isinstance(forbidden, str) and forbidden in text:
                    self._semantic_event("cross_user_context", probe_id)
                if token in user_prompt:
                    self._semantic_event("challenge_seen", probe_id)

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
        tool_call = self._semantic_tool_call(body)
        if tool_call is not None:
            return {"role": "assistant", "content": None, "tool_calls": [tool_call]}
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
            method, raw_path, _version = lines[0].split(" ", 2)
            path = _route_path(raw_path)
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
                self._observe_semantic_context(body)
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
                self._observe_semantic_context(body)
                await self._simulate_latency()
                content = self._response_content(body)
                tool_call = self._semantic_tool_call(body)
                tool_function = tool_call.get("function") if tool_call else None
                if tool_call is not None and isinstance(tool_function, dict):
                    output = [
                        {
                            "type": "function_call",
                            "call_id": tool_call["id"],
                            "name": tool_function["name"],
                            "arguments": tool_function["arguments"],
                            "status": "completed",
                        }
                    ]
                else:
                    tool_call = None
                    output = [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    ]
                payload = {
                    "id": f"resp_{secrets.token_hex(24)}",
                    "object": "response",
                    "status": "completed",
                    "created_at": int(time.time()),
                    "model": self._echo_model(body),
                    "output": output,
                    "output_text": "" if tool_call is not None else content,
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
            elif self._surface == "tool":
                status, payload = self._tool_response(method, raw_path, body)
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

    def _tool_response(
        self, method: str, raw_path: str, body: bytes
    ) -> tuple[str, dict[str, object]]:
        """Mirror the scorer broker's authorization and empty-body preflight."""
        if _route_path(raw_path) != f"/v1/tools/{self._tool_route}/tool":
            return "404 Not Found", {"error": "tool route not found"}
        query = parse_qs(urlsplit(raw_path).query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            return "401 Unauthorized", {"error": "tool route unavailable"}
        case_id = query.get("case_id", [""])[0]
        user_id = query.get("user_id", [""])[0]
        if method == "GET":
            case_id, user_id = "health", ""
        expected = tool_capability(self._tool_key or b"", case_id, user_id)
        if not hmac.compare_digest(query.get("cap", [""])[0], expected):
            return "401 Unauthorized", {"error": "tool route unavailable"}
        if method == "GET":
            return "204 No Content", {}
        if method not in {"HEAD", "POST"}:
            return "405 Method Not Allowed", {"error": "method not allowed"}
        try:
            call = json.loads(body)
        except (UnicodeError, ValueError):
            return "400 Bad Request", {"error": "invalid tool request"}
        if not isinstance(call, dict):
            return "400 Bad Request", {"error": "invalid tool request"}
        if call.get("case_id") != case_id or call.get("user_id") != user_id:
            return "401 Unauthorized", {"error": "tool route unavailable"}
        probe = self._semantic_probe()
        if probe is not None and probe.get("kind") == "memory":
            challenges = probe.get("challenges")
            if isinstance(challenges, list):
                for challenge in challenges:
                    if isinstance(challenge, dict):
                        probe_id = challenge.get("probe_id")
                        forbidden = challenge.get("forbidden")
                        if (
                            isinstance(probe_id, str)
                            and isinstance(forbidden, str)
                            and forbidden in _as_text(body)
                        ):
                            self._semantic_event("cross_user_context", probe_id)
        if probe is not None and probe.get("kind") == "tool":
            probe_id = probe.get("probe_id")
            if (
                not isinstance(probe_id, str)
                or probe.get("case_id") != case_id
                or probe.get("user_id") != user_id
                or call.get("name") != probe.get("name")
                or call.get("args") != probe.get("args")
            ):
                return "409 Conflict", {"error": "tool call was not model-backed"}
            if (
                _SEMANTIC_TOOL_EMISSIONS.get(probe_id, 0) == 0
                or probe_id in _SEMANTIC_TOOL_EXECUTED
            ):
                return "409 Conflict", {"error": "tool call was not model-backed"}
            _SEMANTIC_TOOL_EXECUTED.add(probe_id)
            self._semantic_event("executed", probe_id)
            return "200 OK", {"result": probe["result"], "error": ""}
        # This is a benign execution sink; only model turns count as evidence.
        return "200 OK", {"result": "ok", "error": ""}

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


def _user_prompt_text(body: bytes) -> str:
    """Read user-facing model input, never tool schema or metadata fields."""
    try:
        request = json.loads(body)
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(request, dict):
        return ""

    def content_text(content: object) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)

    messages = request.get("messages")
    if isinstance(messages, list):
        return " ".join(
            content_text(item.get("content"))
            for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
        )
    response_input = request.get("input")
    if isinstance(response_input, str):
        return response_input
    if isinstance(response_input, list):
        return " ".join(
            content_text(item.get("content"))
            for item in response_input
            if isinstance(item, dict) and item.get("role") == "user"
        )
    return ""


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
    semantic_config_file = os.environ.get("DITTO_FAKE_GATEWAY_SEMANTIC_CONFIG")
    semantic_events_file = os.environ.get("DITTO_FAKE_GATEWAY_SEMANTIC_EVENTS")
    latency = _sidecar_latency_range()
    chat_gateway = FakeModelGateway(
        response_text=response_text,
        oracle_answer=oracle_answer,
        host="0.0.0.0",
        port=11435,
        state_file=state_file,
        latency_range=latency,
        surface="model",
        semantic_config_file=semantic_config_file,
        semantic_events_file=semantic_events_file,
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
    tool_gateway = FakeModelGateway(
        host="0.0.0.0",
        port=11436,
        surface="tool",
        tool_route=os.environ["DITTO_FAKE_GATEWAY_TOOL_ROUTE"],
        tool_key=bytes.fromhex(os.environ["DITTO_FAKE_GATEWAY_TOOL_KEY"]),
        semantic_config_file=semantic_config_file,
        semantic_events_file=semantic_events_file,
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
            semantic_config_file=semantic_config_file,
            semantic_events_file=semantic_events_file,
        )
        if tls_context is not None
        else None
    )
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(chat_gateway)
        await stack.enter_async_context(embed_gateway)
        await stack.enter_async_context(tool_gateway)
        if tls_gateway is not None:
            await stack.enter_async_context(tls_gateway)
        await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover - exercised in Docker E2E
    asyncio.run(_serve_sidecar())

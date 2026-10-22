"""Scripted model, embedding, and tool gateway for report-only probe review.

This file is mounted into the locked-down sidecar beside a submitted harness and
must stay standard-library only. It serves the same surfaces as
``ditto_screener.fake_gateway`` (chat completions, Responses, Ollama/OpenAI
embeddings, the scorer-shaped tool route, and the openrouter.ai TLS shim), but
instead of a fixed nonce the reviewer dictates what "the model" and every tool
return through ``/state/script.json``. Every request and reply is appended to
``/state/transcript.jsonl`` so a probe carries the full inference transcript.

Script format (all keys optional)::

    {
      "rules":   [{"when": {...}, "reply": {...}, "max_uses": 1}],
      "turns":   [{...reply...}, ...],          # consumed in order
      "default": {...reply...},
      "tools":   {"<name>": {"result": "...", "error": "", "http_status": 200}},
      "tool_default": {"result": "ok", "error": ""}
    }

A reply is ``{"content", "tool_calls", "finish_reason", "http_status",
"error", "reasoning"}``. ``tool_calls[].arguments`` may be an object or a raw
string, so a reviewer can dictate malformed tool JSON. ``when`` supports
``prompt_contains``, ``has_tools``, ``has_tool_result``, ``tool_choice_set``
and ``turn``. Content may use ``{{last_tool_result}}`` and ``{{user_input}}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 4 * 1024 * 1024
_TRANSCRIPT_BODY_CAP = 256 * 1024
_EMBED_DIMENSIONS = 768
_DEFAULT_ECHO_MODEL = "gpt-4o-mini"
_CHAT_ROUTES = {"/v1/chat/completions", "/chat/completions", "/api/v1/chat/completions"}
_RESPONSE_ROUTES = {"/v1/responses", "/responses", "/api/v1/responses"}
_EMBED_ROUTES = {
    "/api/embed",
    "/api/embeddings",
    "/v1/embeddings",
    "/api/v1/embeddings",
}
# Unicode-aware, so non-ASCII words contribute to the hashed embedding.
_WORD = re.compile(r"\w+")


def _transcript_reply(payload: dict[str, Any] | bytes) -> Any:
    """Record a reply bounded like request bodies, whatever its type."""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")[:_TRANSCRIPT_BODY_CAP]
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= _TRANSCRIPT_BODY_CAP:
        return payload
    return {"_truncated": encoded[:_TRANSCRIPT_BODY_CAP]}


class _State:
    """Per-probe script cursor and append-only transcript, shared by surfaces."""

    def __init__(self, state_dir: str) -> None:
        self.dir = Path(state_dir)
        self.script_path = self.dir / "script.json"
        self.transcript_path = self.dir / "transcript.jsonl"
        self._epoch = ""
        self._script: dict = {}
        self._turn = 0
        self._model_turn = 0
        self._rule_uses: dict[int, int] = {}
        self._tool_uses: dict[str, int] = {}
        self._seq = 0

    def script(self) -> dict:
        """Reload the script whenever the runner writes a new probe epoch."""
        try:
            raw = self.script_path.read_bytes()
        except OSError:
            return self._script
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self._epoch:
            self._epoch = digest
            try:
                value = json.loads(raw)
            except ValueError:
                value = {}
            self._script = value if isinstance(value, dict) else {}
            self._turn = 0
            self._model_turn = 0
            self._rule_uses = {}
            self._tool_uses = {}
        return self._script

    def record(self, entry: dict) -> None:
        self._seq += 1
        entry = {"seq": self._seq, "t": round(time.time(), 3), **entry}
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        fd = os.open(
            self.transcript_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def next_reply(self, request: dict, text: str) -> dict:
        script = self.script()
        self._model_turn += 1
        for index, rule in enumerate(script.get("rules") or []):
            if not isinstance(rule, dict):
                continue
            limit = rule.get("max_uses")
            used = self._rule_uses.get(index, 0)
            if isinstance(limit, int) and used >= limit:
                continue
            when = rule.get("when") or {}
            if not isinstance(when, dict):
                continue  # a malformed condition never matches
            if _matches(when, request, text, self._model_turn):
                self._rule_uses[index] = used + 1
                return dict(rule.get("reply") or {})
        turns = script.get("turns") or []
        if self._turn < len(turns):
            self._turn += 1
            reply = turns[self._turn - 1]
            return dict(reply) if isinstance(reply, dict) else {"content": str(reply)}
        default = script.get("default")
        if isinstance(default, dict):
            return dict(default)
        return {"content": "Answer: unknown"}

    def tool_reply(self, name: str) -> dict:
        script = self.script()
        tools = script.get("tools") or {}
        spec = tools.get(name) if isinstance(tools, dict) else None
        if isinstance(spec, list):
            uses = self._tool_uses.get(name, 0)
            self._tool_uses[name] = uses + 1
            spec = spec[min(uses, len(spec) - 1)] if spec else None
        if not isinstance(spec, dict):
            spec = script.get("tool_default") or {"result": "ok", "error": ""}
        return dict(spec)


def _messages_text(request: dict) -> str:
    return json.dumps(request.get("messages") or request.get("input") or "")


def _last_user_text(request: dict) -> str:
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else json.dumps(content)
    value = request.get("input")
    return value if isinstance(value, str) else json.dumps(value or "")


def _last_tool_result(request: dict) -> str:
    """Tool output of the CURRENT turn: after the last user message.

    Few-shot transcripts often embed earlier ``tool`` messages; those are not
    a result of the call being decided now.
    """
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                return ""
            if message.get("role") == "tool":
                content = message.get("content")
                return content if isinstance(content, str) else json.dumps(content)
    items = request.get("input")
    if isinstance(items, list):
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if item.get("role") == "user":
                return ""
            if item.get("type") == "function_call_output":
                return str(item.get("output", ""))
    return ""


def _matches(when: dict, request: dict, text: str, turn: int) -> bool:
    for key, value in when.items():
        if key == "prompt_contains" and str(value) not in text:
            return False
        if key == "prompt_lacks" and str(value) in text:
            return False
        if key == "has_tools" and bool(request.get("tools")) != bool(value):
            return False
        if key == "has_tool_result" and bool(_last_tool_result(request)) != bool(value):
            return False
        if key == "tool_choice_set" and (
            request.get("tool_choice") not in (None, "auto")
        ) != bool(value):
            return False
        if key == "turn" and turn != value:
            return False
    return True


def _render(content: object, request: dict) -> object:
    if not isinstance(content, str):
        return content
    return content.replace("{{last_tool_result}}", _last_tool_result(request)).replace(
        "{{user_input}}", _last_user_text(request)
    )


def _arguments(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


def _embedding(text: str) -> list[float]:
    """Deterministic hashed bag-of-words vector so retrieval stays meaningful."""
    vector = [0.0] * _EMBED_DIMENSIONS
    for word in _WORD.findall(text.casefold()):
        slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
        vector[slot % _EMBED_DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    if norm == 1.0 and not any(vector):
        vector[0] = 1.0
    return [v / norm for v in vector]


def _embed_inputs(request: dict) -> list[str]:
    value = request.get("input", request.get("prompt", ""))
    if isinstance(value, list):
        return [str(item) for item in value] or [""]
    return [str(value)]


class ScriptedGateway:
    def __init__(
        self,
        state: _State,
        *,
        port: int,
        surface: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._state = state
        self._port = port
        self._surface = surface
        self._ssl = ssl_context
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> ScriptedGateway:
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self._port, ssl=self._ssl
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _chat(self, request: dict) -> tuple[int, dict | bytes, str]:
        text = _messages_text(request)
        reply = self._state.next_reply(request, text)
        status = int(reply.get("http_status", 200))
        if status != 200:
            return (
                status,
                {"error": {"message": reply.get("error", "upstream error")}},
                "application/json",
            )
        content = _render(reply.get("content"), request)
        message: dict = {"role": "assistant", "content": content}
        calls = []
        for call in reply.get("tool_calls") or []:
            calls.append(
                {
                    "id": f"call_{secrets.token_hex(12)}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": _arguments(call.get("arguments", {})),
                    },
                }
            )
        if calls:
            message["tool_calls"] = calls
        if reply.get("reasoning"):
            message["reasoning_content"] = reply["reasoning"]
        finish = reply.get("finish_reason") or ("tool_calls" if calls else "stop")
        model = request.get("model") or _DEFAULT_ECHO_MODEL
        usage = {"prompt_tokens": max(1, len(text) // 4), "completion_tokens": 8}
        usage["total_tokens"] = usage["prompt_tokens"] + 8
        cid = f"chatcmpl-{secrets.token_hex(12)}"
        if request.get("stream"):
            delta: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if calls:
                delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(calls)]
            chunks: list[dict[str, Any]] = [
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": usage,
                },
            ]
            body = b"".join(
                b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks
            )
            return 200, body + b"data: [DONE]\n\n", "text/event-stream"
        return (
            200,
            {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            },
            "application/json",
        )

    def _responses(self, request: dict) -> tuple[int, dict, str]:
        text = _messages_text(request)
        reply = self._state.next_reply(request, text)
        status = int(reply.get("http_status", 200))
        if status != 200:
            return (
                status,
                {"error": {"message": reply.get("error", "")}},
                "application/json",
            )
        content = _render(reply.get("content"), request)
        output: list[dict] = []
        for call in reply.get("tool_calls") or []:
            output.append(
                {
                    "type": "function_call",
                    "call_id": f"call_{secrets.token_hex(12)}",
                    "name": call.get("name", ""),
                    "arguments": _arguments(call.get("arguments", {})),
                    "status": "completed",
                }
            )
        if content is not None and not output:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            )
        return (
            200,
            {
                "id": f"resp_{secrets.token_hex(24)}",
                "object": "response",
                "status": "completed",
                "created_at": int(time.time()),
                "model": request.get("model") or _DEFAULT_ECHO_MODEL,
                "output": output,
                "output_text": "" if reply.get("tool_calls") else (content or ""),
                "usage": {"input_tokens": max(1, len(text) // 4), "output_tokens": 8},
            },
            "application/json",
        )

    def _tool(self, request: dict) -> tuple[int, dict, str]:
        name = str(request.get("name", ""))
        spec = self._state.tool_reply(name)
        status = int(spec.get("http_status", 200))
        payload = {
            "result": _render(spec.get("result", "ok"), request),
            "error": spec.get("error", ""),
        }
        for key in ("operation_id", "effect_state"):
            if key in spec:
                payload[key] = spec[key]
        return status, payload, "application/json"

    def route(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict | bytes, str, str]:
        try:
            request = json.loads(body) if body else {}
        except (ValueError, UnicodeError):
            request = {"_unparsed": body[:2048].decode("utf-8", "replace")}
        if not isinstance(request, dict):
            request = {"_value": request}
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}, "application/json", "health"
        if self._surface in {"model", "all"} and method == "POST":
            if path in _CHAT_ROUTES:
                return (*self._chat(request), "chat")
            if path in _RESPONSE_ROUTES:
                return (*self._responses(request), "responses")
        if (
            self._surface in {"embedding", "all"}
            and method == "POST"
            and path in _EMBED_ROUTES
        ):
            vectors = [_embedding(text) for text in _embed_inputs(request)]
            return (
                200,
                {
                    "model": request.get("model") or "nomic-embed-text",
                    "embedding": vectors[0],
                    "embeddings": vectors,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": v}
                        for i, v in enumerate(vectors)
                    ],
                },
                "application/json",
                "embedding",
            )
        if self._surface in {"tool", "model", "all"} and path.startswith("/v1/tools/"):
            if method in {"GET", "HEAD"}:
                return 204, b"", "application/json", "tool_health"
            if method != "POST":
                # Only a POST invokes a tool; nothing else consumes a reply.
                return (
                    405,
                    {"error": {"message": "method not allowed"}},
                    "application/json",
                    "tool_method",
                )
            return (*self._tool(request), "tool")
        return (
            404,
            {"error": {"message": "Unrecognized request URL."}},
            "application/json",
            "unknown",
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        payload: dict[str, Any] | bytes = {"error": "bad request"}
        status, ctype, surface = 400, "application/json", "invalid"
        method, raw_path, body = "", "", b""
        try:
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=10
            )
            if len(raw_headers) > _MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            lines = raw_headers.decode("latin-1").split("\r\n")
            method, raw_path, _ = lines[0].split(" ", 2)
            headers = {
                k.strip().casefold(): v.strip()
                for line in lines[1:]
                if ":" in line
                for k, v in [line.split(":", 1)]
            }
            if headers.get("expect", "").casefold() == "100-continue":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            body = await _read_body(reader, headers)
            path = raw_path.split("?", 1)[0].rstrip("/") or "/"
            status, payload, ctype, surface = self.route(method, path, body)
        except (
            TimeoutError,
            ValueError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            OSError,
        ) as error:
            payload = {
                "error": {"message": f"malformed request: {type(error).__name__}"}
            }
        if surface not in {"health", "tool_health"}:
            try:
                parsed = json.loads(body) if body else None
            except (ValueError, UnicodeError):
                parsed = body[:_TRANSCRIPT_BODY_CAP].decode("utf-8", "replace")
            encoded = json.dumps(parsed, default=str)
            if len(encoded) > _TRANSCRIPT_BODY_CAP:
                parsed = {"_truncated": encoded[:_TRANSCRIPT_BODY_CAP]}
            self._state.record(
                {
                    "surface": surface,
                    "method": method,
                    "path": raw_path,
                    "status": status,
                    "request": parsed,
                    "reply": _transcript_reply(payload),
                }
            )
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":")).encode()
        )
        reason = {
            200: "OK",
            204: "No Content",
            404: "Not Found",
            429: "Too Many Requests",
        }.get(status, "Status")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
            + data
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    if "chunked" in headers.get("transfer-encoding", "").casefold():
        body = bytearray()
        while True:
            size_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=10)
            size = int(size_line[:-2].split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while (
                    await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=10)
                ) != b"\r\n":
                    pass
                return bytes(body)
            if len(body) + size > _MAX_BODY_BYTES:
                raise ValueError("body too large")
            body.extend(await asyncio.wait_for(reader.readexactly(size), timeout=10))
            await asyncio.wait_for(reader.readexactly(2), timeout=10)
    length = int(headers.get("content-length", "0") or 0)
    if length < 0 or length > _MAX_BODY_BYTES:
        raise ValueError("body too large")
    return (
        await asyncio.wait_for(reader.readexactly(length), timeout=10)
        if length
        else b""
    )


async def _serve() -> None:
    state = _State(os.environ.get("PROBE_GATEWAY_STATE_DIR", "/state"))
    tls = None
    cert = os.environ.get("PROBE_GATEWAY_TLS_CERT")
    key = os.environ.get("PROBE_GATEWAY_TLS_KEY")
    if cert and key and Path(cert).exists():
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
    gateways = [
        ScriptedGateway(state, port=11435, surface="model"),
        ScriptedGateway(state, port=11434, surface="embedding"),
        ScriptedGateway(state, port=11436, surface="tool"),
    ]
    if tls is not None:
        gateways.append(
            ScriptedGateway(state, port=443, surface="all", ssl_context=tls)
        )
    async with contextlib.AsyncExitStack() as stack:
        for gateway in gateways:
            await stack.enter_async_context(gateway)
        await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover - runs inside the sidecar
    asyncio.run(_serve())

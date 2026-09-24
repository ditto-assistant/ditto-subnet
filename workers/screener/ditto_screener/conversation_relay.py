"""Credential-isolated, single-assessment inference sidecar (stdlib only).

The harness reaches this process on its internal Docker network. Only this
trusted sidecar also joins an egress network. No caller selects an upstream,
model, provider credential, budget, or administrative route.
"""

from __future__ import annotations

import base64
import json
import math
import os
import signal
import ssl
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

MODEL = "openai/gpt-oss-20b"
EMBED_MODEL = "perplexity/pplx-embed-v1-0.6b"
PROFILE = "conversation-openrouter-oss20b-pplx768-v1"
MAX_BODY = 131_072
MAX_RESPONSE = 8_388_608
BUDGET = 5_000_000
MAX_REQUESTS = 300


class RelayError(ValueError):
    """Only fixed, source-owned codes may be persisted as private diagnostics."""


class DrainingHTTPServer(ThreadingHTTPServer):
    """Wait for every request handler before writing the settled ledger."""

    daemon_threads = False
    block_on_close = True


class Relay:
    def __init__(self, key: str, state_file: Path):
        self.key = key
        self.state_file = state_file
        self.lock = threading.Lock()
        self.spent = 0
        self.requests = 0
        self.chat_dispatches = 0
        self.successful_chat_responses = 0
        self.settled = False
        self.tokens = 0
        self.unmetered = False
        self.cost_is_upper_bound = False
        self.failed = False
        self.failure: dict[str, Any] | None = None
        self.save()

    def fail(self, exc: Exception, stage: str) -> None:
        # Caller holds the dispatch lock. Preserve the original error when an
        # untrusted client retries; never store exception text, URLs or bodies.
        self.failed = True
        if self.failure is None:
            code = "unexpected_error"
            status = None
            if isinstance(exc, RelayError):
                code = str(exc)
            elif isinstance(exc, HTTPError):
                code, status = "provider_http_error", exc.code
            elif isinstance(exc, TimeoutError):
                code = "timeout"
            elif isinstance(exc, json.JSONDecodeError):
                code = "invalid_json"
            elif isinstance(exc, (KeyError, TypeError, IndexError)):
                code = "invalid_response_shape"
            elif isinstance(exc, OSError):
                code = "transport_error"
            self.failure = {"code": code, "stage": stage, "http_status": status}

    def save(self) -> None:
        # The host owns this pre-created file; only the trusted relay mounts it.
        # Read it after stopping the sidecar, never midway through an update.
        with self.state_file.open("w") as handle:
            handle.write(
                json.dumps(
                    {
                        "profile": PROFILE,
                        "requests": self.requests,
                        "chat_dispatches": self.chat_dispatches,
                        "successful_chat_responses": self.successful_chat_responses,
                        "settled": self.settled,
                        "tokens": self.tokens,
                        "spent_microusd": self.spent,
                        "unmetered": self.unmetered,
                        "failed": self.failed,
                        "cost_is_upper_bound": self.cost_is_upper_bound,
                        "failure": self.failure,
                    }
                )
            )
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def payload(path: str, body: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function"
            for tool in tools
        ):
            raise RelayError("only_client_side_function_tools_are_supported")
        if path in {"/v1/responses", "/responses", "/api/v1/responses"}:
            inputs = body.get("input")
            if not isinstance(inputs, (str, list)) or body.get("previous_response_id"):
                raise RelayError("stateless_text_input_required")
            if isinstance(inputs, list):
                for item in inputs:
                    if not isinstance(item, dict) or item.get(
                        "type", "message"
                    ) not in {
                        "message",
                        "function_call",
                        "function_call_output",
                        "reasoning",
                    }:
                        raise RelayError("unsupported_response_input")
                    content = item.get("content")
                    if isinstance(content, list) and any(
                        not isinstance(part, dict)
                        or part.get("type") not in {"input_text", "output_text"}
                        for part in content
                    ):
                        raise RelayError("text_only_response_input_required")
            output = body.get("max_output_tokens", 4096)
            if type(output) is not int or output < 1 or body.get("stream"):
                raise RelayError("invalid_response_output_limit")
            output = min(output, 8192)
            response_payload: dict[str, Any] = {
                k: body[k]
                for k in (
                    "input",
                    "instructions",
                    "tools",
                    "tool_choice",
                    "parallel_tool_calls",
                    "text",
                )
                if k in body
            }
            response_payload.update(
                model=MODEL,
                max_output_tokens=output,
                store=False,
                stream=False,
                reasoning={"effort": "medium"},
                provider={
                    "allow_fallbacks": False,
                    "data_collection": "deny",
                    "max_price": {"prompt": 2, "completion": 2},
                },
            )
            return "responses", response_payload, False
        embed = path in {
            "/api/embed",
            "/api/embeddings",
            "/v1/embeddings",
            "/api/v1/embeddings",
        }
        if embed:
            inputs = body.get("input", body.get("prompt"))
            if isinstance(inputs, str):
                inputs = [inputs]
            if (
                not isinstance(inputs, list)
                or not 1 <= len(inputs) <= 128
                or not all(isinstance(s, str) for s in inputs)
            ):
                raise RelayError("invalid_embedding_input")
            return (
                "embeddings",
                {
                    "model": EMBED_MODEL,
                    "input": inputs,
                    "dimensions": 768,
                    "encoding_format": "float",
                    "provider": {
                        "only": ["Perplexity"],
                        "allow_fallbacks": False,
                        "data_collection": "deny",
                        "max_price": {"prompt": 2},
                    },
                },
                True,
            )
        if path not in {
            "/v1/chat/completions",
            "/chat/completions",
            "/api/v1/chat/completions",
        }:
            raise RelayError("unsupported_inference_route")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
            raise RelayError("invalid_messages")
        # Text-only instrument: remote images/audio and provider extensions can
        # introduce unbounded cost or retrieval outside the conversation.
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }:
                raise RelayError("invalid_message")
            content = message.get("content")
            if isinstance(content, list):
                # Rig's OpenAI client serializes system text as content parts.
                # Preserve the text and part boundaries, with no image/audio or
                # provider extensions allowed through this text-only lane.
                if any(
                    not isinstance(part, dict)
                    or set(part) != {"type", "text"}
                    or part["type"] != "text"
                    or not isinstance(part["text"], str)
                    for part in content
                ):
                    raise RelayError("unsupported_message_content")
            elif content is not None and not isinstance(content, str):
                raise RelayError("unsupported_message_content")
        output = body.get("max_completion_tokens", body.get("max_tokens", 4096))
        if type(output) is not int or output < 1:
            raise RelayError("invalid_output_limit")
        # Clients may advertise a larger allowance (e.g. 16k). The instrument
        # owns the actual ceiling and reserves only this bounded dispatch.
        output = min(output, 8192)
        effort = body.get("reasoning_effort", "medium")
        if effort not in {"low", "medium", "high"}:
            raise RelayError("invalid_reasoning_effort")
        payload: dict[str, Any] = {
            k: body[k]
            for k in (
                "messages",
                "tools",
                "tool_choice",
                "temperature",
                "top_p",
                "stop",
                "response_format",
            )
            if k in body
        }
        payload.update(
            {
                "model": MODEL,
                "stream": False,
                "max_tokens": output,
                "reasoning": {"effort": effort},
                "provider": {
                    "allow_fallbacks": False,
                    "data_collection": "deny",
                    "max_price": {"prompt": 2, "completion": 2},
                },
                "usage": {"include": True},
            }
        )
        if body.get("stream"):
            raise RelayError("streaming_is_not_supported_by_this_instrument")
        return "chat/completions", payload, False

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            route, payload, embed = self.payload(path, body)
        except Exception as exc:
            with self.lock:
                self.fail(exc, "request")
                self.save()
            raise
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        input_bound = len(encoded) + 8192
        output_bound = (
            0
            if embed
            else int(payload.get("max_tokens", payload.get("max_output_tokens", 0)))
        )
        # OpenRouter max_price is dollars per million tokens; 2 microUSD/token
        # is the corresponding route ceiling, applied before every dispatch.
        reservation = 2 * (input_bound + output_bound)
        reservation = (reservation * 105 + 99) // 100
        with self.lock:
            if (
                self.failed
                or self.requests >= MAX_REQUESTS
                or self.spent + reservation > BUDGET
            ):
                raise RelayError("inference_budget_unavailable")
            self.spent += reservation
            self.requests += 1
            if not embed:
                self.chat_dispatches += 1
            self.unmetered = True
            self.save()  # Persist before a possibly billed dispatch.
            try:
                request = urllib.request.Request(
                    "https://openrouter.ai/api/v1/" + route,
                    data=encoded,
                    headers={
                        "Authorization": "Bearer " + self.key,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect()
                )
                with opener.open(request, timeout=110) as response:
                    data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise RelayError("oversized_provider_response")
                result = json.loads(data)
                usage = result["usage"]
                prompt = usage.get(
                    "prompt_tokens",
                    usage.get("total_tokens") if embed else usage.get("input_tokens"),
                )
                completion = (
                    0
                    if embed
                    else usage.get("completion_tokens", usage.get("output_tokens"))
                )
                cost = usage.get("cost")
                if (
                    usage.get("is_byok") is True
                    and type(prompt) is int
                    and type(completion) is int
                ):
                    # BYOK credits cover the Router fee, not the provider bill.
                    # Keep the provider price ceiling as a labelled upper bound.
                    tariff = 2 * (prompt + completion) / 1_000_000
                    if cost is None:
                        cost = tariff * 1.05
                    elif (
                        type(cost) in {int, float} and math.isfinite(cost) and cost >= 0
                    ):
                        cost += tariff
                    else:
                        raise RelayError("unverifiable_byok_fee")
                    self.cost_is_upper_bound = True
                if embed and cost is None and type(prompt) is int:
                    # Embeddings can omit dollars. Preserve a clearly labelled
                    # upper bound at the enforced provider price ceiling.
                    cost = prompt * 2 / 1_000_000
                    self.cost_is_upper_bound = True
                if (
                    type(prompt) is not int
                    or not 0 <= prompt <= input_bound
                    or type(completion) is not int
                    or not 0 <= completion <= output_bound
                    or type(cost) not in {int, float}
                    or not math.isfinite(cost)
                    or not 0 <= cost * 1_000_000 <= reservation
                ):
                    raise RelayError("unverifiable_provider_usage")
                self.spent += math.ceil(cost * 1_000_000) - reservation
                self.tokens += prompt + completion
                self.unmetered = False
                if not embed:
                    self.successful_chat_responses += 1
                if embed and path in {"/api/embed", "/api/embeddings"}:
                    vectors = [
                        item["embedding"]
                        for item in sorted(result["data"], key=lambda i: i["index"])
                    ]
                    if any(len(v) != 768 for v in vectors):
                        raise RelayError("embedding_dimension_mismatch")
                    result = {
                        "model": body.get("model", EMBED_MODEL),
                        "embeddings": vectors,
                        "embedding": vectors[0],
                        "prompt_eval_count": prompt,
                    }
                return result
            except Exception as exc:
                self.fail(exc, "provider")  # Never issue a paid fallback or retry.
                raise
            finally:
                self.save()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def harness_request(
    path: str, body: bytes | None, *, timeout: int = 120
) -> tuple[int, bytes]:
    """Fixed-target host ingress; never forwards a caller's headers or origin."""
    if (body is None and path != "/health") or (
        body is not None and (path not in {"/run", "/seed"} or len(body) > MAX_BODY)
    ):
        raise RelayError("unsupported_harness_route")
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise RelayError("invalid_harness_timeout")
    request = urllib.request.Request(
        "http://agent:8080" + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read(64_001)
        if len(data) > 64_000:
            raise RelayError("oversized_harness_response")
        return response.status, data


def harness_stdio() -> None:
    """Trusted Docker-exec transport; no published host port or caller headers."""
    try:
        raw = sys.stdin.buffer.read(200_001)
        if len(raw) > 200_000:
            raise RelayError("oversized_harness_envelope")
        envelope = json.loads(raw)
        body = envelope["body"]
        decoded = base64.b64decode(body, validate=True) if body is not None else None
        status, data = harness_request(
            envelope["path"], decoded, timeout=envelope["timeout"]
        )
        result = {"status": status, "body": base64.b64encode(data).decode()}
    except Exception as exc:
        # Return only a fixed vocabulary over trusted stdout. Never expose an
        # exception string, URL, response body, or traceback from the sandbox.
        if isinstance(exc, TimeoutError) or (
            isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
        ):
            code = "timeout"
        elif isinstance(exc, (URLError, ConnectionError)):
            code = "connection_failed"
        elif isinstance(exc, RelayError) and str(exc) == "oversized_harness_response":
            code = "response_too_large"
        else:
            code = "transport_failed"
        result = {"error": code}
    print(json.dumps(result))


def serve(relay: Relay) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Provider and harness bodies/headers never enter daemon logs.

        def do_GET(self) -> None:
            payload: dict[str, Any]
            if self.path in {"/v1/models", "/api/v1/models"}:
                payload = {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
            elif self.path == "/api/tags":
                payload = {"models": [{"name": EMBED_MODEL, "model": EMBED_MODEL}]}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            self.connection.settimeout(120)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise RelayError("invalid_request_size")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise RelayError("truncated_request")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise RelayError("invalid_body")
                payload = relay.post(self.path, body)
                status = 200
            except Exception as exc:
                with relay.lock:
                    relay.fail(exc, "request")
                    relay.save()
                payload = {"error": {"message": "conversation inference unavailable"}}
                status = 502
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    # Serial dispatch plus a bounded socket backlog contains malicious fan-out.
    private_case = os.environ.get("DITTO_PRIVATE_CASE") == "1"
    server_type = DrainingHTTPServer if private_case else ThreadingHTTPServer
    server = server_type(("0.0.0.0", 11434), Handler)
    tls = server_type(("0.0.0.0", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/private/leaf.crt", "/private/leaf.key")
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    chat = server_type(("0.0.0.0", 11435), Handler)
    servers = [server, tls, chat]
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    for listener in servers:
        threading.Thread(target=listener.serve_forever, daemon=True).start()
    try:
        stopped.wait()
    finally:
        for listener in servers:
            listener.shutdown()
        # Join in-flight handlers before the host reads settled usage. Docker's
        # outer stop deadline still bounds hostile or stalled connections.
        for listener in servers:
            listener.server_close()
        if private_case:
            with relay.lock:
                relay.settled = True
                relay.save()


if __name__ == "__main__":
    if sys.argv[1:] == ["harness"]:
        harness_stdio()
    elif not sys.argv[1:]:
        serve(Relay(os.environ["OPENROUTER_API_KEY"], Path("/state/usage.json")))
    else:
        raise SystemExit("unsupported relay mode")

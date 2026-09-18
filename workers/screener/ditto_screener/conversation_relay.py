"""Credential-isolated, single-assessment inference sidecar (stdlib only).

The harness reaches this process on its internal Docker network. Only this
trusted sidecar also joins an egress network. No caller selects an upstream,
model, provider credential, budget, or administrative route.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "openai/gpt-oss-20b"
EMBED_MODEL = "perplexity/pplx-embed-v1-0.6b"
PROFILE = "conversation-openrouter-oss20b-pplx768-v1"
MAX_BODY = 131_072
MAX_RESPONSE = 8_388_608
BUDGET = 5_000_000
MAX_REQUESTS = 300


class Relay:
    def __init__(self, key: str, state_file: Path):
        self.key = key
        self.state_file = state_file
        self.lock = threading.Lock()
        self.spent = 0
        self.requests = 0
        self.tokens = 0
        self.unmetered = False
        self.cost_is_upper_bound = False
        self.failed = False
        self.save()

    def save(self) -> None:
        # The host owns this pre-created file; only the trusted relay mounts it.
        # Read it after stopping the sidecar, never midway through an update.
        with self.state_file.open("w") as handle:
            handle.write(
                json.dumps(
                    {
                        "profile": PROFILE,
                        "requests": self.requests,
                        "tokens": self.tokens,
                        "spent_microusd": self.spent,
                        "unmetered": self.unmetered,
                        "failed": self.failed,
                        "cost_is_upper_bound": self.cost_is_upper_bound,
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
            raise ValueError("only client-side function tools are supported")
        if path in {"/v1/responses", "/responses", "/api/v1/responses"}:
            inputs = body.get("input")
            if not isinstance(inputs, (str, list)) or body.get("previous_response_id"):
                raise ValueError("stateless text input required")
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
                        raise ValueError("unsupported response input")
                    content = item.get("content")
                    if isinstance(content, list) and any(
                        not isinstance(part, dict)
                        or part.get("type") not in {"input_text", "output_text"}
                        for part in content
                    ):
                        raise ValueError("text-only response input required")
            output = body.get("max_output_tokens", 4096)
            if type(output) is not int or not 1 <= output <= 8192 or body.get("stream"):
                raise ValueError("invalid response output limit")
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
                raise ValueError("invalid embedding input")
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
            raise ValueError("unsupported inference route")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
            raise ValueError("invalid messages")
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
                raise ValueError("invalid message")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("only text messages are supported")
        output = body.get("max_completion_tokens", body.get("max_tokens", 4096))
        if type(output) is not int or not 1 <= output <= 8192:
            raise ValueError("invalid output limit")
        effort = body.get("reasoning_effort", "medium")
        if effort not in {"low", "medium", "high"}:
            raise ValueError("invalid reasoning effort")
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
            raise ValueError("streaming is not supported by this instrument")
        return "chat/completions", payload, False

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        route, payload, embed = self.payload(path, body)
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
        with self.lock:
            if (
                self.failed
                or self.requests >= MAX_REQUESTS
                or self.spent + reservation > BUDGET
            ):
                raise ValueError("inference budget unavailable")
            self.spent += reservation
            self.requests += 1
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
                    raise ValueError("oversized provider response")
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
                    raise ValueError("unverifiable provider usage")
                self.spent += math.ceil(cost * 1_000_000) - reservation
                self.tokens += prompt + completion
                self.unmetered = False
                if embed and path in {"/api/embed", "/api/embeddings"}:
                    vectors = [
                        item["embedding"]
                        for item in sorted(result["data"], key=lambda i: i["index"])
                    ]
                    if any(len(v) != 768 for v in vectors):
                        raise ValueError("embedding dimension mismatch")
                    result = {
                        "model": body.get("model", EMBED_MODEL),
                        "embeddings": vectors,
                        "embedding": vectors[0],
                        "prompt_eval_count": prompt,
                    }
                return result
            except Exception:
                self.failed = True  # Never issue a paid fallback or retry.
                raise
            finally:
                self.save()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


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
                    raise ValueError("invalid request size")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("truncated request")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("invalid body")
                payload = relay.post(self.path, body)
                status = 200
            except Exception:
                with relay.lock:
                    relay.failed = True
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
    server = ThreadingHTTPServer(("0.0.0.0", 11434), Handler)
    server.daemon_threads = True
    tls = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/private/leaf.crt", "/private/leaf.key")
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    chat = ThreadingHTTPServer(("0.0.0.0", 11435), Handler)
    threading.Thread(target=chat.serve_forever, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    serve(Relay(os.environ["OPENROUTER_API_KEY"], Path("/state/usage.json")))

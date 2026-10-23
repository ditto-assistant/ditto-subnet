#!/usr/bin/env python3
"""Narrow OpenAI-compatible proxy for LongMemEval's official judge.

The upstream evaluator hard-codes the official judge name, so this proxy is the
only seam where the model actually answering can be changed without editing
LongMemEval itself. It defaults to the frozen GPT-4o judge and is byte-identical
in that configuration.

Setting ``LONGMEMEVAL_JUDGE_TARGET_MODEL`` points the same official prompts at a
different model. That is strictly a research instrument: a run whose target is
not the official model is NOT an official-protocol LongMemEval score and must
not be reported as one. ``/health`` reports the target so a run can be audited
after the fact rather than trusted.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


OFFICIAL_MODEL = "gpt-4o-2024-08-06"
OFFICIAL_OPENROUTER_MODEL = "openai/gpt-4o-2024-08-06"


def target_model() -> str:
    """The model that actually answers. Defaults to the official judge."""
    return (
        os.environ.get("LONGMEMEVAL_JUDGE_TARGET_MODEL", "").strip()
        or OFFICIAL_OPENROUTER_MODEL
    )


def is_official() -> bool:
    return target_model() == OFFICIAL_OPENROUTER_MODEL
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
MAX_BODY_BYTES = 1024 * 1024


def rewrite_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    if value.get("model") != OFFICIAL_MODEL:
        raise ValueError(f"judge model must be {OFFICIAL_MODEL}")
    if value.get("stream") is True:
        raise ValueError("streaming is not supported")
    rewritten = dict(value)
    rewritten["model"] = target_model()
    rewritten["stream"] = False
    return rewritten


class Handler(BaseHTTPRequestHandler):
    server_version = "LongMemEvalJudgeProxy/1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"judge-proxy {self.address_string()} {format % args}", flush=True)

    def write_json(self, status: int, value: Any) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.write_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "profile_revision": (
                    "longmemeval-official-gpt4o-openrouter-v1"
                    if is_official()
                    else "longmemeval-judge-substitution-research-v1"
                ),
                "official_model": OFFICIAL_MODEL,
                "upstream_model": target_model(),
                "official_protocol": is_official(),
            },
        )

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY_BYTES:
            self.write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"})
            return
        try:
            request_value = json.loads(self.rfile.read(length))
            body = json.dumps(rewrite_request(request_value)).encode("utf-8")
        except (json.JSONDecodeError, ValueError) as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        upstream = urllib.request.Request(
            UPSTREAM,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.server.api_key}",  # type: ignore[attr-defined]
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/xiaowu0162/LongMemEval",
                "X-Title": "LongMemEval official evaluator",
            },
        )
        try:
            with urllib.request.urlopen(upstream, timeout=180) as response:
                response_body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get_content_type())
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
        except urllib.error.URLError as exc:
            self.write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"judge upstream unavailable: {exc.reason}"})


def main() -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    port = int(os.environ.get("PORT", "18436"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.api_key = api_key  # type: ignore[attr-defined]
    model = target_model()
    if is_official():
        print(f"LongMemEval judge proxy on :{port}; official judge {model}", flush=True)
    else:
        print(
            f"LongMemEval judge proxy on :{port}; RESEARCH SUBSTITUTION {model} "
            f"(not an official-protocol score)",
            flush=True,
        )
    server.serve_forever()


if __name__ == "__main__":
    main()

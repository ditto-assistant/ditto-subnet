#!/usr/bin/env python3
"""Narrow OpenAI-compatible proxy for the LongMemEval judge SN118 scores with.

The judge is ``deepseek/deepseek-v4.1-flash`` pinned to one provider, matching
``internal/longmemeval/provider_runtime.go``. The evaluator that calls this
proxy is the Ditto fork recorded in ``judge_evaluator.py``; the unmodified
upstream evaluator is deliberately rejected, because its ``max_tokens=10``
makes a reasoning judge return an empty completion that upstream silently
labels "no" on every question.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_evaluator  # noqa: E402  (sibling module: the frozen judge identity)


JUDGE_MODEL = judge_evaluator.JUDGE_MODEL
JUDGE_MAX_COMPLETION_TOKENS = judge_evaluator.JUDGE_MAX_COMPLETION_TOKENS
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"
MAX_BODY_BYTES = 1024 * 1024


def rewrite_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    if value.get("model") != JUDGE_MODEL:
        raise ValueError(f"judge model must be {JUDGE_MODEL}")
    if value.get("stream") is True:
        raise ValueError("streaming is not supported")
    # A bound below the frozen one is the silent all-"no" failure: a reasoning
    # judge spends the budget on reasoning tokens, returns content="", and an
    # evaluator that labels by `'yes' in response` scores every question "no"
    # without raising anything. Refuse it at the boundary rather than serving a
    # run whose 0% looks like a model that cannot do the task.
    max_tokens = value.get("max_tokens")
    if max_tokens != JUDGE_MAX_COMPLETION_TOKENS:
        raise ValueError(
            f"judge max_tokens must be {JUDGE_MAX_COMPLETION_TOKENS}, got "
            f"{max_tokens!r}; an unmodified upstream evaluator sends 10, which "
            'silently labels every truncated verdict "no"'
        )
    rewritten = dict(value)
    rewritten["model"] = JUDGE_MODEL
    rewritten["stream"] = False
    # The provider pin is part of the frozen contract, not a preference.
    rewritten["provider"] = judge_evaluator.judge_provider_routing()
    return rewritten


class Handler(BaseHTTPRequestHandler):
    server_version = "LongMemEvalJudgeProxy/2"

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
                "profile_revision": judge_evaluator.JUDGE_PROFILE_REVISION,
                "judge_model": JUDGE_MODEL,
                "upstream_model": JUDGE_MODEL,
                "max_completion_tokens": JUDGE_MAX_COMPLETION_TOKENS,
                "provider": judge_evaluator.judge_provider_routing(),
                # A substituted judge is not a LongMemEval score. Any consumer
                # that reports a number from this lane must carry that through.
                "official_protocol": judge_evaluator.OFFICIAL_PROTOCOL,
                "official_judge_retired": judge_evaluator.OFFICIAL_JUDGE_RETIREMENT,
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
                "HTTP-Referer": judge_evaluator.FORK_REPOSITORY,
                "X-Title": "LongMemEval evaluator (Ditto judge fork)",
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
    print(
        f"LongMemEval judge proxy on :{port}; model pinned to {JUDGE_MODEL} "
        f"@{judge_evaluator.JUDGE_ROUTE_PROVIDER}, bound "
        f"{JUDGE_MAX_COMPLETION_TOKENS}; NOT an official-protocol judge",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

"""Private warm Cloud Run service for one credential-minimal review at a time."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ditto_screener.source_review_job import _amain

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 32_000
_MAX_ENV_VALUE_BYTES = 8_192
_REVIEW_LOCK = threading.Lock()
_REVIEW_ENV_NAMES = frozenset(
    {
        "DITTO_PLATFORM_URL",
        "DITTO_SOURCE_REVIEW_ID",
        "DITTO_SOURCE_REVIEW_ATTEMPT_ID",
        "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256",
        "DITTO_SOURCE_REVIEW_JOB_TOKEN",
        "DITTO_SOURCE_REVIEW_JOB",
        "SCREENER_NODE_CREDENTIAL_FILE",
        "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN",
        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
        "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS",
        "SCREENER_SOURCE_REVIEW_MAX_STEPS",
        "SCREENER_SOURCE_REVIEW_MAX_READ_BYTES",
        "SCREENER_SOURCE_REVIEW_REASONING_EFFORT",
        "SCREENER_SOURCE_REVIEW_MODEL",
        "SCREENER_SOURCE_REVIEW_FALLBACK_MODELS",
        "SCREENER_L2_REVIEW_MODE",
        "SCREENER_L2_REVIEW_MODEL",
        "SCREENER_L2_FALLBACK_MODELS",
        "SCREENER_L3_REVIEW_ENABLED",
        "SCREENER_L3_REVIEW_MODEL",
        "SCREENER_L2_TIMEOUT_SECONDS",
        "SCREENER_L2_MAX_STEPS",
        "SCREENER_L2_MAX_INPUT_TOKENS",
        "SCREENER_L2_MAX_OUTPUT_TOKENS",
        "SCREENER_L2_MAX_COMPLETION_TOKENS",
        "SCREENER_L2_MAX_COST_USD",
        "SCREENER_L2_CRITIC_REASONING_EFFORT",
        "SCREENER_L2_CACHE_TTL_SECONDS",
        "SCREENER_L2_AUDIT_RETENTION_DAYS",
        "SCREENER_REVIEW_CONCERN_HOLD_COUNT",
        "SCREENER_REVIEW_CLEAR_MIN_NOTES",
    }
)


def _parse_env(payload: object) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("request must be an object")
    rows = payload.get("env")
    if not isinstance(rows, list) or len(rows) > len(_REVIEW_ENV_NAMES):
        raise ValueError("request environment is invalid")
    parsed: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
        ):
            raise ValueError("request environment entry is invalid")
        name, value = row
        if name not in _REVIEW_ENV_NAMES or name in parsed:
            raise ValueError("request environment name is invalid")
        if len(value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
            raise ValueError("request environment value is too large")
        parsed[name] = value
    required = {
        "DITTO_PLATFORM_URL",
        "DITTO_SOURCE_REVIEW_ID",
        "DITTO_SOURCE_REVIEW_ATTEMPT_ID",
        "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256",
        "DITTO_SOURCE_REVIEW_JOB_TOKEN",
        "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN",
        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
    }
    if not required.issubset(parsed):
        raise ValueError("request environment is incomplete")
    return parsed


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


class _Handler(BaseHTTPRequestHandler):
    server_version = "ditto-source-review/1"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/review":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not _REVIEW_LOCK.acquire(blocking=False):
            self.send_error(HTTPStatus.CONFLICT)
            return
        try:
            raw_length = self.headers.get("Content-Length", "")
            try:
                length = int(raw_length)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if length < 1 or length > _MAX_BODY_BYTES:
                self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                env = _parse_env(payload)
            except (UnicodeDecodeError, ValueError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            try:
                with _temporary_environment(env):
                    asyncio.run(_amain(linger_seconds=0))
            except Exception:
                logger.exception("warm source review request failed")
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        finally:
            _REVIEW_LOCK.release()

    def log_message(self, format: str, *args: object) -> None:
        logger.info(format, *args)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()

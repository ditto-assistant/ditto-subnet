"""Minimal HTTP server exposing the DittoBench harness contract."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .protocol import ProtocolError, RunRequest, SeedRequest
from .runtime import HermesRunner
from .state import HermesStateStore


MAX_REQUEST_BYTES = 256 * 1024 * 1024


def state_root() -> Path:
    configured = Path(os.environ.get("DITTOBENCH_DB", "/tmp/dittobench.db"))
    return configured.parent / "dittobench-hermes"


class AdapterService:
    def __init__(self, state: HermesStateStore | None = None, runner: HermesRunner | None = None) -> None:
        self.state = state or HermesStateStore(state_root())
        self.runner = runner or HermesRunner(self.state)

    def seed(self, value: Any) -> dict[str, int]:
        return self.state.seed(SeedRequest.from_json(value))

    def run(self, value: Any) -> dict[str, Any]:
        return self.runner.run(RunRequest.from_json(value))


class Handler(BaseHTTPRequestHandler):
    service: AdapterService

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)

    def _write(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ProtocolError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ProtocolError("request body is empty or too large")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ProtocolError("request body is not valid JSON") from exc

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write(HTTPStatus.OK, {"status": "ok", "adapter": "hermes", "version": __version__})
            return
        self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            if self.path == "/seed":
                self._write(HTTPStatus.OK, self.service.seed(body))
                return
            if self.path == "/run":
                self._write(HTTPStatus.OK, self.service.run(body))
                return
            self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ProtocolError as exc:
            self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    Handler.service = AdapterService()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    finally:
        Handler.service.state.close()


if __name__ == "__main__":
    main()

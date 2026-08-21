"""Fault-injection reverse proxy in front of a relay or OpenRouter.

Cheatcodes ``inject_provider`` and ``drop_relay`` are read from preview-control
on every request so localstack and a preview stack share the same cranks.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx


class FaultProxy:
    """HTTP proxy that consults preview-control before forwarding."""

    def __init__(
        self,
        control_url: str,
        upstream: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.control_url = control_url.rstrip("/")
        self.upstream = upstream.rstrip("/")
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        bound_host, bound_port = self._httpd.server_address[:2]
        return f"http://{bound_host}:{bound_port}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _faults(self) -> tuple[int | None, bool]:
        response = httpx.get(f"{self.control_url}/v1/state", timeout=2.0)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("provider_status")
        dropped = bool(payload.get("relay_dropped"))
        return status, dropped

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self._forward()

            def do_POST(self) -> None:  # noqa: N802
                self._forward()

            def do_PUT(self) -> None:  # noqa: N802
                self._forward()

            def _forward(self) -> None:
                try:
                    status, dropped = proxy._faults()
                except Exception as exc:  # noqa: BLE001
                    self._write(
                        502, json.dumps({"error": f"control unreachable: {exc}"})
                    )
                    return
                if dropped:
                    self._write(
                        502, json.dumps({"error": "relay dropped by preview-control"})
                    )
                    return
                if status is not None:
                    self._write(
                        int(status),
                        json.dumps(
                            {"error": "injected by preview-control", "status": status}
                        ),
                    )
                    return
                parsed = urlparse(proxy.upstream)
                path = self.path
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length else b""
                conn: HTTPConnection | HTTPSConnection
                if parsed.scheme == "https":
                    conn = HTTPSConnection(
                        parsed.hostname or "", parsed.port or 443, timeout=30
                    )
                else:
                    conn = HTTPConnection(
                        parsed.hostname or "", parsed.port or 80, timeout=30
                    )
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "content-length"}
                }
                try:
                    conn.request(self.command, path, body=body, headers=headers)
                    upstream = conn.getresponse()
                    payload = upstream.read()
                    self.send_response(upstream.status)
                    for key, value in upstream.getheaders():
                        if key.lower() in {"transfer-encoding", "connection"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    conn.close()

            def _write(self, status: int, payload: str) -> None:
                blob = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        return Handler

"""Fault-injection reverse proxy in front of a relay or OpenRouter.

Cheatcodes ``inject_provider`` and ``drop_relay`` are read from preview-control
on every request so localstack and a preview stack share the same cranks.
"""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from http.client import HTTPConnection, HTTPException, HTTPSConnection
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
        *,
        control_token: str = "",
    ):
        if host != "127.0.0.1":
            raise ValueError("fault proxy only binds to loopback")
        if not control_token:
            raise ValueError("fault proxy requires a preview-control token")
        self.control_url = control_url.rstrip("/")
        self.upstream = upstream.rstrip("/")
        self.control_token = control_token
        parsed = urlparse(self.upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("fault proxy upstream must be an http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("fault proxy upstream must not contain credentials")
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        bound_host, bound_port = self._httpd.server_address[:2]
        host = bound_host.decode() if isinstance(bound_host, bytes) else str(bound_host)
        return f"http://{host}:{int(bound_port)}"

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
        headers = (
            {"Authorization": f"Bearer {self.control_token}"}
            if self.control_token
            else {}
        )
        response = httpx.get(
            f"{self.control_url}/v1/state", headers=headers, timeout=2.0
        )
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
                requested = urlparse(self.path)
                if requested.scheme or requested.netloc:
                    self._write(
                        400,
                        json.dumps({"error": "absolute request targets are refused"}),
                    )
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._write(
                        411,
                        json.dumps({"error": "chunked requests are not supported"}),
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self._write(400, json.dumps({"error": "invalid Content-Length"}))
                    return
                if length < 0 or length > 16 * 1024 * 1024:
                    self._write(413, json.dumps({"error": "request body is too large"}))
                    return
                body = self.rfile.read(length) if length else b""
                base = parsed.path.rstrip("/")
                suffix = (
                    requested.path
                    if requested.path.startswith("/")
                    else f"/{requested.path}"
                )
                path = f"{base}{suffix}" or "/"
                if requested.query:
                    path = f"{path}?{requested.query}"
                conn: HTTPConnection | HTTPSConnection
                if parsed.scheme == "https":
                    conn = HTTPSConnection(
                        parsed.hostname or "", parsed.port or 443, timeout=30
                    )
                else:
                    conn = HTTPConnection(
                        parsed.hostname or "", parsed.port or 80, timeout=30
                    )
                connection_headers = {
                    item.strip().lower()
                    for item in self.headers.get("Connection", "").split(",")
                    if item.strip()
                }
                hop_by_hop = {
                    "connection",
                    "content-length",
                    "host",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailer",
                    "transfer-encoding",
                    "upgrade",
                } | connection_headers
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in hop_by_hop
                }
                response_started = False
                try:
                    conn.request(self.command, path, body=body, headers=headers)
                    upstream = conn.getresponse()
                    self.send_response(upstream.status)
                    for key, value in upstream.getheaders():
                        if key.lower() in {
                            "connection",
                            "content-length",
                            "keep-alive",
                            "proxy-authenticate",
                            "te",
                            "trailer",
                            "transfer-encoding",
                            "upgrade",
                        }:
                            continue
                        self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    response_started = True
                    self.close_connection = True
                    while chunk := upstream.read1(64 * 1024):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (HTTPException, OSError, TimeoutError) as exc:
                    if not response_started and not self.wfile.closed:
                        with suppress(OSError):
                            self._write(
                                502,
                                json.dumps({"error": f"upstream unreachable: {exc}"}),
                            )
                    self.close_connection = True
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

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx

from ditto.preview.client import PreviewClient
from ditto.preview.engine import PreviewEngine
from ditto.preview.orchestrator import up
from ditto.preview.proxy import FaultProxy
from ditto.preview.server import PreviewServer

HOTKEY = "5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY"


def test_http_cheatcodes_roundtrip() -> None:
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    server = PreviewServer(engine, host="127.0.0.1", port=0)
    server.start()
    try:
        client = PreviewClient(server.url)
        assert client.health()["ok"] is True
        client.register(HOTKEY, permit=True, stake=2)
        client.warp_block(3)
        lease = client.issue_lease(HOTKEY, lifetime_blocks=2)
        client.warp_block(2)
        state = client.state()
        assert state["block"] == 6
        assert state["neurons"][0]["permit"] is True
        assert state["leases"][0]["lease_id"] == lease["lease_id"]
        assert state["leases"][0]["expired"] is True
        grant = client.issue_grant()
        client.exhaust_allowance(grant["grant_id"])
        client.inject_provider(503)
        assert client.state()["provider_status"] == 503
        client.inject_provider(None)
        client.snapshot("a")
        client.warp_block(9)
        client.revert("a")
        assert client.state()["block"] == 6
    finally:
        server.stop()


def test_fault_proxy_injects_429_then_forwards() -> None:
    upstream = _Upstream()
    upstream.start()
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    control = PreviewServer(engine, host="127.0.0.1", port=0)
    control.start()
    proxy = FaultProxy(control.url, upstream.url, host="127.0.0.1", port=0)
    proxy.start()
    try:
        client = PreviewClient(control.url)
        client.inject_provider(429)
        denied = httpx.get(proxy.url + "/v1/chat/completions", timeout=2)
        assert denied.status_code == 429
        client.inject_provider(None)
        ok = httpx.get(proxy.url + "/v1/chat/completions", timeout=2)
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
        client.drop_relay(True)
        dropped = httpx.get(proxy.url + "/health", timeout=2)
        assert dropped.status_code == 502
    finally:
        proxy.stop()
        control.stop()
        upstream.stop()


def test_up_stack_writes_urls_and_control() -> None:
    handle = up(["stack"], ref="feat/x", sha="abc1234567890fff")
    try:
        assert handle.plan.localnet_validator is True
        assert handle.urls["control"].startswith("http://127.0.0.1:")
        assert "fault_proxy" in handle.urls
        client = PreviewClient(handle.urls["control"])
        assert client.health()["ok"] is True
    finally:
        handle.down()


class _Upstream:
    def __init__(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                blob = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{name}:{int(port)}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

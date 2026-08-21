from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

import ditto.preview.orchestrator as orchestrator
from ditto.preview.client import PreviewClient
from ditto.preview.engine import IsolationError, PreviewEngine
from ditto.preview.orchestrator import up
from ditto.preview.proxy import FaultProxy
from ditto.preview.server import PreviewServer

HOTKEY = "5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY"


def test_http_cheatcodes_roundtrip() -> None:
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    server = PreviewServer(engine, host="127.0.0.1", port=0)
    server.start()
    try:
        client = PreviewClient(server.url, token=server.token)
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


def test_http_control_requires_auth_json_and_loopback() -> None:
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    with pytest.raises(IsolationError, match="loopback"):
        PreviewServer(engine, host="0.0.0.0", port=0)

    server = PreviewServer(engine, host="127.0.0.1", port=0)
    server.start()
    try:
        unauthorized = httpx.post(
            server.url + "/v1/cheat/warp_block",
            content='{"n": 5}',
            headers={"Content-Type": "text/plain"},
        )
        assert unauthorized.status_code == 401
        assert engine.block == 1

        wrong_type = httpx.post(
            server.url + "/v1/cheat/warp_block",
            content='{"n": 5}',
            headers={
                "Authorization": f"Bearer {server.token}",
                "Content-Type": "text/plain",
            },
        )
        assert wrong_type.status_code == 400
        assert engine.block == 1
    finally:
        server.stop()


def test_fault_proxy_injects_429_then_forwards() -> None:
    upstream = _Upstream()
    upstream.start()
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    control = PreviewServer(engine, host="127.0.0.1", port=0)
    control.start()
    proxy = FaultProxy(
        control.url,
        upstream.url,
        host="127.0.0.1",
        port=0,
        control_token=control.token,
    )
    proxy.start()
    try:
        client = PreviewClient(control.url, token=control.token)
        client.inject_provider(429)
        denied = httpx.get(proxy.url + "/v1/chat/completions", timeout=2)
        assert denied.status_code == 429
        client.inject_provider(None)
        ok = httpx.get(proxy.url + "/v1/chat/completions", timeout=2)
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
        assert upstream.paths[-1] == "/v1/chat/completions"
        client.drop_relay(True)
        dropped = httpx.get(proxy.url + "/health", timeout=2)
        assert dropped.status_code == 502
    finally:
        proxy.stop()
        control.stop()
        upstream.stop()


def test_fault_proxy_preserves_base_path_and_bounds_upstream_failure() -> None:
    upstream = _Upstream()
    upstream.start()
    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    control = PreviewServer(engine, host="127.0.0.1", port=0)
    control.start()
    proxy = FaultProxy(
        control.url,
        upstream.url + "/base",
        host="127.0.0.1",
        port=0,
        control_token=control.token,
    )
    dead = FaultProxy(
        control.url,
        "http://127.0.0.1:1/base",
        host="127.0.0.1",
        port=0,
        control_token=control.token,
    )
    proxy.start()
    dead.start()
    try:
        response = httpx.get(proxy.url + "/child?q=1", timeout=2)
        assert response.status_code == 200
        assert upstream.paths[-1] == "/base/child?q=1"
        unavailable = httpx.get(dead.url + "/child", timeout=2)
        assert unavailable.status_code == 502
        assert "upstream unreachable" in unavailable.text
    finally:
        dead.stop()
        proxy.stop()
        control.stop()
        upstream.stop()


def test_up_stack_writes_urls_and_control() -> None:
    handle = up(["stack"], ref="feat/x", sha="abc1234567890fff")
    try:
        assert handle.plan.localnet_validator is True
        assert handle.urls["control"].startswith("http://127.0.0.1:")
        assert "fault_proxy" in handle.urls
        client = PreviewClient(handle.urls["control"], token=handle.control_token)
        assert client.health()["ok"] is True
    finally:
        handle.down()


def test_up_unwinds_servers_when_compose_start_fails(monkeypatch) -> None:
    controls: list[PreviewServer] = []
    proxies: list[FaultProxy] = []

    class RecordingServer(PreviewServer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            controls.append(self)

    class RecordingProxy(FaultProxy):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            proxies.append(self)

    def fail_compose(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["docker", "compose"])

    monkeypatch.setattr(orchestrator, "PreviewServer", RecordingServer)
    monkeypatch.setattr(orchestrator, "FaultProxy", RecordingProxy)
    monkeypatch.setattr(orchestrator.subprocess, "run", fail_compose)

    with pytest.raises(subprocess.CalledProcessError):
        orchestrator.up(
            ["stack"],
            ref="feat/fail",
            sha="abc1234567890fff",
            start_postgres=True,
        )
    assert controls and controls[0]._thread is None
    assert proxies and proxies[0]._thread is None


class _Upstream:
    def __init__(self) -> None:
        self.paths: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                owner.paths.append(self.path)
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

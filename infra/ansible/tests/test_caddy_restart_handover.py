"""Caddy holds requests across a `ditto-api` restart instead of 502ing (SN-426).

`ditto-api` is a single fork-mode process, so a deploy refuses connections for
a few seconds. These tests run the rendered Caddyfile against an upstream that
is not listening yet and comes up mid-request, the way pm2 brings the new
process up, and against an upstream that accepts a request and then dies, which
must not be replayed.
"""

import hashlib
import http.client
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = (
    ROOT / "infra/ansible/roles/platform_app/templates/Caddyfile.j2"
).read_text()
RESTART_GAP_SECONDS = 1.5


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _render(port: int, api_port: int, relays: list[int], standby_port: int = 0) -> str:
    env = Environment()
    env.filters["bool"] = bool
    rendered = env.from_string(TEMPLATE).render(
        platform_caddy_email="ops@example.com",
        platform_domain=f"http://127.0.0.1:{port}",
        platform_alias_domains=[],
        platform_inference_relay_ports=relays,
        platform_upload_admission_relay_enabled=False,
        platform_public_rate_limit_per_minute=0,
        platform_api_port=api_port,
        platform_request_body_max_size="1MiB",
        platform_api_restart_try_duration="20s",
        platform_api_standby_port=standby_port,
    )
    return rendered.replace(
        "email ops@example.com", "admin off\n    persist_config off"
    )


def _serve(port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Echo(BaseHTTPRequestHandler):
    def _reply(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(hashlib.sha256(body).hexdigest().encode())

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def _run_caddy(tmp_path: Path, port: int, rendered: str) -> Iterator[None]:
    binary = shutil.which("caddy")
    if not binary:
        pytest.fail("Caddy is required for the restart-handover integration tests")
    config = tmp_path / "Caddyfile"
    config.write_text(
        rendered.replace(
            "/opt/ditto/logs/caddy/platform.log", str(tmp_path / "access.log")
        )
    )
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
    }
    with (tmp_path / "caddy.log").open("w+") as log:
        process = subprocess.Popen(
            [binary, "run", "--config", str(config), "--adapter", "caddyfile"],
            stdout=log,
            stderr=log,
            env=env,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Caddy did not start on loopback")
            yield
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.fixture(params=["direct", "relay"])
def caddy(request, tmp_path) -> Iterator[tuple[int, int]]:
    port, api_port, relay_port = _free_port(), _free_port(), _free_port()
    relays = [relay_port] if request.param == "relay" else []
    with _run_caddy(tmp_path, port, _render(port, api_port, relays)):
        yield port, api_port


@pytest.fixture(params=["direct", "relay"])
def standby_caddy(request, tmp_path) -> Iterator[tuple[int, int, int]]:
    """Primary-first proxying to ``ditto-api`` and its warm standby."""
    port, api_port, standby_port = _free_port(), _free_port(), _free_port()
    relays = [_free_port()] if request.param == "relay" else []
    rendered = _render(port, api_port, relays, standby_port=standby_port)
    with _run_caddy(tmp_path, port, rendered):
        yield port, api_port, standby_port


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "GET",
            "/api/v1/validator/agent/00000000-0000-0000-0000-000000000001/artifact",
            None,
        ),
        (
            "POST",
            "/api/v1/validator/tickets/hand-back",
            b'{"reason":"infrastructure"}' * 64,
        ),
    ],
)
def test_request_during_restart_waits_for_the_new_process(caddy, method, path, body):
    port, api_port = caddy
    servers: list[ThreadingHTTPServer] = []
    starter = threading.Timer(
        RESTART_GAP_SECONDS, lambda: servers.append(_serve(api_port, _Echo))
    )
    started = time.monotonic()
    starter.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        connection.request(method, path, body)
        response = connection.getresponse()
        result = response.read()
        connection.close()
    finally:
        starter.join()
        for server in servers:
            server.shutdown()
            server.server_close()
    assert response.status == 200, result
    assert result.decode() == hashlib.sha256(body or b"").hexdigest()
    assert time.monotonic() - started >= RESTART_GAP_SECONDS - 0.2


def test_request_the_old_process_accepted_is_not_replayed(caddy):
    port, api_port = caddy
    received: list[str] = []

    class _DiesMidRequest(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *args: object) -> None:
            pass

    server = _serve(api_port, _DiesMidRequest)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        connection.request("POST", "/api/v1/validator/agent/x/score", b"{}")
        response = connection.getresponse()
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
    assert response.status == 502
    assert received == ["/api/v1/validator/agent/x/score"]


def _answering(name: str, served: list[str]) -> type[BaseHTTPRequestHandler]:
    class _Named(_Echo):
        def _reply(self) -> None:
            served.append(name)
            super()._reply()

        do_GET = _reply
        do_POST = _reply

    return _Named


def test_standby_answers_while_the_primary_restarts(standby_caddy):
    port, _api_port, standby_port = standby_caddy
    served: list[str] = []
    standby = _serve(standby_port, _answering("standby", served))
    started = time.monotonic()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        connection.request("POST", "/api/v1/validator/tickets/hand-back", b"{}")
        response = connection.getresponse()
        result = response.read()
        connection.close()
    finally:
        standby.shutdown()
        standby.server_close()
    assert response.status == 200, result
    assert served == ["standby"]
    # Failover, not the restart handover's wait for the primary to return.
    assert time.monotonic() - started < RESTART_GAP_SECONDS


def test_request_waits_when_primary_and_standby_are_both_down(standby_caddy):
    port, api_port, _standby_port = standby_caddy
    served: list[str] = []
    servers: list[ThreadingHTTPServer] = []
    starter = threading.Timer(
        RESTART_GAP_SECONDS,
        lambda: servers.append(_serve(api_port, _answering("primary", served))),
    )
    started = time.monotonic()
    starter.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        result = response.read()
        connection.close()
    finally:
        starter.join()
        for server in servers:
            server.shutdown()
            server.server_close()
    assert response.status == 200, result
    assert served == ["primary"]
    assert time.monotonic() - started >= RESTART_GAP_SECONDS - 0.2


def test_request_the_primary_accepted_is_not_replayed_on_the_standby(standby_caddy):
    port, api_port, standby_port = standby_caddy
    received: list[str] = []
    served: list[str] = []

    class _DiesMidRequest(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *args: object) -> None:
            pass

    primary = _serve(api_port, _DiesMidRequest)
    standby = _serve(standby_port, _answering("standby", served))
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
        connection.request("POST", "/api/v1/validator/agent/x/score", b"{}")
        response = connection.getresponse()
        response.read()
        connection.close()
    finally:
        for server in (primary, standby):
            server.shutdown()
            server.server_close()
    assert response.status == 502
    assert received == ["/api/v1/validator/agent/x/score"]
    assert served == []

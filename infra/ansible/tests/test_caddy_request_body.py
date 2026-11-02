"""Caddy bounds bytes forwarded upstream and formats edge 413 responses."""

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = (
    ROOT / "infra/ansible/roles/platform_app/templates/Caddyfile.j2"
).read_text()


def _render(**overrides: object) -> str:
    context = {
        "platform_caddy_email": "ops@example.com",
        "platform_domain": "platform.example.com",
        "platform_alias_domains": [],
        "platform_inference_relay_ports": [],
        "platform_upload_admission_relay_enabled": False,
        "platform_public_rate_limit_per_minute": 0,
        "platform_api_port": 8000,
        "platform_request_body_max_size": "32MiB",
    }
    context.update(overrides)
    env = Environment()
    env.filters["bool"] = bool
    return env.from_string(TEMPLATE).render(context)


def _directive_line(rendered: str, prefix: str) -> int:
    for index, line in enumerate(rendered.splitlines()):
        if line.lstrip().startswith(prefix):
            return index
    raise AssertionError(prefix)


_EDGE_ENVELOPE = (
    '{"error_code":3004,"message":"request body exceeds 32MiB","request_id":"{err.id}"}'
)


def test_direct_proxy_caps_the_body() -> None:
    rendered = _render()
    assert "max_size 32MiB" in rendered
    assert _directive_line(rendered, "request_body {") < _directive_line(
        rendered, "reverse_proxy "
    )
    assert rendered.count("handle_errors 413 {") == 1
    assert _EDGE_ENVELOPE in rendered
    assert "header Connection close" in rendered


def test_relay_handles_each_cap_the_body() -> None:
    rendered = _render(
        platform_inference_relay_ports=[9001, 9002],
        platform_upload_admission_relay_enabled=True,
    )
    assert rendered.count("request_body {") == 3
    assert rendered.count("max_size 32MiB") == 3
    assert rendered.count("handle_errors 413 {") == 1
    assert rendered.count(_EDGE_ENVELOPE) == 1


def test_custom_cap_is_applied_to_every_proxy() -> None:
    rendered = _render(
        platform_inference_relay_ports=[9001, 9002],
        platform_upload_admission_relay_enabled=True,
        platform_request_body_max_size="65MiB",
    )
    assert rendered.count("max_size 65MiB") == 3
    assert "32MiB" not in rendered


@pytest.fixture
def upstream():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(bytearray())
            body = received[-1]
            if self.headers.get("Transfer-Encoding") == "chunked":
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    size = int(line.strip(), 16)
                    if not size:
                        break
                    chunk = self.rfile.read(size)
                    body.extend(chunk)
                    if len(chunk) < size:
                        return
                    self.rfile.read(2)
            else:
                body.extend(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(hashlib.sha256(body).hexdigest().encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # An oversized streaming request closes the upstream.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, received
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(
    params=["direct", "relay", "rate-limited", "preview-platform", "preview-dashboard"]
)
def proxy(request, upstream, tmp_path):
    caddy = shutil.which("caddy")
    if not caddy:
        pytest.fail("Caddy is required for the request-body integration tests")
    upstream_port, received = upstream
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    hostname = "127.0.0.1"
    if request.param.startswith("preview-"):
        hostname = request.param.removeprefix("preview-") + ".localhost"
        rendered = (ROOT / "preview/cloud/Caddyfile").read_text()
        rendered = rendered.replace("{$PREVIEW_BASE_DOMAIN}", "localhost")
        for site in ("dashboard", "platform", "backroom"):
            rendered = rendered.replace(
                f"{site}.localhost {{", f"http://{site}.localhost:{port} {{"
            )
        rendered = rendered.replace("platform:8000", f"127.0.0.1:{upstream_port}")
        rendered = rendered.replace("32MiB", "1KiB")
        rendered = rendered.replace(
            "email preview@dittobench.ai", "admin off\n  persist_config off"
        )
    else:
        relays = [] if request.param == "direct" else [upstream_port]
        rendered = _render(
            platform_domain=f"http://127.0.0.1:{port}",
            platform_api_port=upstream_port,
            platform_inference_relay_ports=relays,
            platform_upload_admission_relay_enabled=True,
            platform_public_rate_limit_per_minute=10
            if request.param == "rate-limited"
            else 0,
            platform_request_body_max_size="1KiB",
        )
        rendered = rendered.replace(
            "email ops@example.com", "admin off\n    persist_config off"
        )
        rendered = rendered.replace(
            "/opt/ditto/logs/caddy/platform.log", str(tmp_path / "access.log")
        )
    config = tmp_path / "Caddyfile"
    config.write_text(rendered)
    # Isolate Caddy's config/storage from the developer's local Caddy instance.
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
    }
    with (tmp_path / "caddy.log").open("w+") as log:
        process = subprocess.Popen(
            [caddy, "run", "--config", str(config), "--adapter", "caddyfile"],
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
            yield port, hostname, received
        finally:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/upload/agent",
        "/api/v1/upload/check",
        "/api/v1/inference/chat/completions",
    ],
)
@pytest.mark.parametrize("chunked", [False, True])
def test_live_proxy_body_boundary(proxy, path, chunked):
    port, hostname, received = proxy
    body = b"x" * 1024
    for oversized in (False, True):
        payload = body + (b"x" if oversized else b"")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            path,
            [payload] if chunked else payload,
            headers={"Host": hostname},
            encode_chunked=chunked,
        )
        response = connection.getresponse()
        result = response.read()
        if not oversized:
            assert response.status == 200
            assert result.decode() == hashlib.sha256(payload).hexdigest()
        else:
            assert response.status == 413
            envelope = json.loads(result)
            assert envelope["error_code"] == 3004
            request_id = envelope["request_id"]
            assert request_id and "{" not in request_id
            assert response.getheader("X-Request-ID") == request_id
            assert response.getheader("Content-Type") == "application/json"
            assert response.getheader("Connection") == "close"
            assert all(len(body) <= 1024 for body in received)
        connection.close()

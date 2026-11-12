"""CLI smoke: ready file, authenticated health, SIGTERM exits 0, refusals exit 2."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from query_adapter_pilot.contract import CONTRACT_VERSION, ROUTE_HEALTH, TOKEN_HEADER

TOKEN = "cli-token"


def _spawn(tmp_path: Path, *extra: str) -> tuple[subprocess.Popen[bytes], Path]:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    ready = tmp_path / "ready.json"
    cmd = [
        sys.executable,
        "-m",
        "query_adapter_pilot.miner",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--token-file",
        str(token_file),
        "--cache-capacity",
        "4",
        "--max-tenants",
        "8",
        "--ready-file",
        str(ready),
        *extra,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, ready


def _wait_ready(proc: subprocess.Popen[bytes], ready: Path) -> dict[str, object]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready.exists():
            return json.loads(ready.read_text())  # type: ignore[no-any-return]
        if proc.poll() is not None:
            raise AssertionError(proc.stderr.read().decode() if proc.stderr else "exited")
        time.sleep(0.05)
    raise AssertionError("miner never wrote its ready file")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_cli_ready_file_health_and_clean_shutdown(tmp_path: Path, sig: signal.Signals) -> None:
    proc, ready = _spawn(tmp_path, "--fault", "slow", "--slow-ms", "1")
    try:
        doc = _wait_ready(proc, ready)
        assert doc["contract"] == CONTRACT_VERSION and doc["pid"] == proc.pid
        url = str(doc["url"])
        assert url.startswith("http://127.0.0.1:")
        u = urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname or "", u.port, timeout=10)
        conn.request("GET", ROUTE_HEALTH, headers={TOKEN_HEADER: TOKEN})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200 and body["fault"] == "slow"
        assert body["cache"]["capacity"] == 4
        os.kill(proc.pid, sig)
        assert proc.wait(timeout=20) == 0
        assert not ready.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize(
    "extra", [("--host", "0.0.0.0"), ("--host", "10.0.0.1"), ("--cache-capacity", "0")]
)
def test_cli_refuses_unsafe_settings(tmp_path: Path, extra: tuple[str, ...]) -> None:
    proc, ready = _spawn(tmp_path, *extra)
    assert proc.wait(timeout=20) == 2
    assert not ready.exists()


def test_cli_refuses_empty_token(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("\n")
    ready = tmp_path / "ready.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "query_adapter_pilot.miner",
            "--token-file",
            str(tmp_path / "token"),
            "--ready-file",
            str(ready),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2 and not ready.exists()

"""Tests for the screener build gate.

The Docker CLI layer (``BuildGate._run``) is stubbed per-test so we exercise the
orchestration (download -> verify -> dockerfile check -> build -> serve smoke ->
teardown) without a real daemon; HTTP (artifact download + /health probe) is a
mocked transport.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from ditto.screener.config import ScreenerConfig
from ditto.screener.gate import BuildGate, GateResult, _log_tail, dockerfile_at_root

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_URL = "https://storage.test/agent.tar.gz"


def _make_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- pure helpers ---------------------------------------------------------


def test_dockerfile_at_root() -> None:
    assert dockerfile_at_root(["Dockerfile", "src/lib.rs"])
    assert dockerfile_at_root(["./Dockerfile", "Cargo.toml"])
    assert not dockerfile_at_root(["sub/Dockerfile", "sub/Cargo.toml"])
    assert not dockerfile_at_root(["Cargo.toml"])


def test_log_tail_trims() -> None:
    assert _log_tail("  hi  ") == "hi"
    long = "x" * 5000
    tail = _log_tail(long)
    assert tail.startswith("…") and len(tail) <= 2001


# --- screen() orchestration ----------------------------------------------


def _gate_with(
    cfg: ScreenerConfig,
    run_stub: Callable[..., Any],
    *,
    tar: bytes,
    health_status: int = 200,
) -> BuildGate:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":  # the /health probe
            return httpx.Response(health_status, json={"status": "ok"})
        return httpx.Response(200, content=tar)  # the artifact download

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gate = BuildGate(cfg, http)
    gate._run = run_stub  # type: ignore[method-assign]
    return gate


def _ok_run(port_line: str = "127.0.0.1:49999") -> Callable[..., Any]:
    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        cmd = args[0]
        if cmd == "build" and stdin is not None:
            stdin.read()  # drain the context like docker would
        if cmd == "port":
            return (0, port_line)
        return (0, "")

    return _run


async def test_pass_builds_and_serves(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"Dockerfile": b"FROM scratch\n", "Cargo.toml": b"[package]"})
    sha = hashlib.sha256(tar).hexdigest()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert res == GateResult(True, "")


async def test_sha_mismatch_fails_before_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"Dockerfile": b"FROM scratch\n"})
    built: list[str] = []

    async def _run(args: list[str], **_: Any) -> tuple[int, str]:
        built.append(args[0])
        return (0, "")

    gate = _gate_with(make_config(), _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256="00" * 32, download_url=_URL)
    assert not res.passed and "sha256 mismatch" in res.detail
    assert "build" not in built  # never attempted


async def test_no_root_dockerfile_fails(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"src/lib.rs": b"fn main() {}", "Cargo.toml": b"[package]"})
    sha = hashlib.sha256(tar).hexdigest()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed and res.detail == "no Dockerfile at tarball root"


async def test_build_failure_reported(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"Dockerfile": b"FROM scratch\n"})
    sha = hashlib.sha256(tar).hexdigest()

    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return (1, "error[E0432]: unresolved import")
        return (0, "")

    gate = _gate_with(make_config(), _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed
    assert "build failed" in res.detail and "unresolved import" in res.detail


async def test_container_start_failure_reported(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"Dockerfile": b"FROM scratch\n"})
    sha = hashlib.sha256(tar).hexdigest()

    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build" and stdin is not None:
            stdin.read()
        if args[0] == "run":
            return (125, "docker: Error response from daemon")
        return (0, "")

    gate = _gate_with(make_config(), _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed and "serve check failed" in res.detail


async def test_unhealthy_serve_times_out(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar({"Dockerfile": b"FROM scratch\n"})
    sha = hashlib.sha256(tar).hexdigest()
    # run_timeout small so the probe loop exits quickly; /health returns 503.
    cfg = make_config(run_timeout_seconds=0.05)
    gate = _gate_with(cfg, _ok_run(), tar=tar, health_status=503)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed and "never healthy" in res.detail

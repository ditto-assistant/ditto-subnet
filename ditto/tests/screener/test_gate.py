"""Tests for the screener build gate.

The Docker CLI layer (``BuildGate._run``) is stubbed per-test so we exercise the
orchestration (download -> verify -> dockerfile check -> build -> serve smoke ->
teardown) without a real daemon; artifact-download HTTP is a mocked transport.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from ditto.screener.config import ScreenerConfig
from ditto.screener.gate import (
    BuildGate,
    GateResult,
    _detail_tail,
    _log_tail,
    dockerfile_at_root,
)
from ditto.screener.model_canary import ModelCallCanary

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


def _valid_tar(**overrides: bytes) -> bytes:
    files = {
        "Dockerfile": b"FROM scratch\n",
        "Cargo.toml": (b'[package]\nname = "agent"\nversion = "0.1.0"\n'),
        "src/main.rs": b"fn main() {}\n",
    }
    files.update(overrides)
    return _make_tar(files)


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
    assert len(_detail_tail("x" * 5000)) == 3900


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

    async def _pass_canary(*_: Any, **__: Any) -> GateResult:
        return GateResult(True, "")

    gate._run_model_canary = _pass_canary  # type: ignore[method-assign]

    return gate


def _ok_run() -> Callable[..., Any]:
    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        cmd = args[0]
        if cmd == "build" and stdin is not None:
            stdin.read()  # drain the context like docker would
        return (0, "")

    return _run


async def test_pass_builds_and_serves(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _valid_tar()
    sha = hashlib.sha256(tar).hexdigest()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert res == GateResult(True, "")


async def test_screening_runs_model_canary(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _valid_tar()
    sha = hashlib.sha256(tar).hexdigest()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)

    calls = 0

    async def _observed_canary(*_: Any, **__: Any) -> GateResult:
        nonlocal calls
        calls += 1
        return GateResult(True, "")

    gate._run_model_canary = _observed_canary  # type: ignore[method-assign]
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert res == GateResult(True, "")
    assert calls == 1


async def test_smoke_env_injected_into_run(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """The dummy LLM key must reach the serve container as ``-e K=V`` so the
    reference harness (LLM Baseline built before /health binds) can boot."""
    tar = _valid_tar()
    sha = hashlib.sha256(tar).hexdigest()
    run_calls: list[list[str]] = []

    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        run_calls.append(args)
        if args[0] == "build" and stdin is not None:
            stdin.read()
        return (0, "")

    cfg = make_config(smoke_env=(("OPENROUTER_API_KEY", "sk-dummy"), ("FOO", "bar")))
    gate = _gate_with(cfg, _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert res.passed
    run_args = next(
        a for a in run_calls if a[0] == "run" and a[-1].startswith("ditto-screen/")
    )
    # The tag stays last; each pair is injected as an "-e K=V" before it.
    assert run_args[-1].startswith("ditto-screen/")
    assert "-e" in run_args
    assert "OPENROUTER_API_KEY=sk-dummy" in run_args
    assert "FOO=bar" in run_args
    assert "--network" in run_args
    assert "--network-alias" in run_args
    alias_index = run_args.index("--network-alias")
    assert run_args[alias_index + 1] == "harness"
    assert "--publish" not in run_args
    assert "DITTOBENCH_PROVIDER=chutes" in run_args
    assert "CHUTES_API_KEY=relay" in run_args
    assert "CHUTES_BASE_URL=http://model-canary:8080/v1" in run_args

    sidecar_args = next(
        a for a in run_calls if a[0] == "run" and "DITTO_CANARY_TOKEN=" in " ".join(a)
    )
    assert "--read-only" in sidecar_args
    assert "--cap-drop" in sidecar_args
    assert "no-new-privileges" in sidecar_args
    assert "--network-alias" in sidecar_args

    network_args = next(a for a in run_calls if a[:2] == ["network", "create"])
    assert "--internal" in network_args

    probe_calls = [a for a in run_calls if a[0] == "exec"]
    assert any("http://harness:8080/health" in a for a in probe_calls)


async def test_model_canary_rejects_harness_that_makes_no_model_call(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _valid_tar()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)
    del gate._run_model_canary  # type: ignore[attr-defined]
    result = await gate._run_model_canary(
        "http://127.0.0.1:8080",
        token="hidden",
        model_called_path="/definitely/missing/model-called",
    )
    await gate._client.aclose()
    assert not result.passed
    assert not result.retryable
    assert result.detail == "model canary observed no model call"


async def test_model_canary_requires_harness_to_use_hidden_response(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    with tempfile.TemporaryDirectory() as state_dir:
        state_file = os.path.join(state_dir, "model-called")
        async with ModelCallCanary(state_file=state_file) as canary:
            local_gateway = canary.gateway_url.replace(
                "host.docker.internal", "127.0.0.1"
            )

            async def handler(request: httpx.Request) -> httpx.Response:
                assert request.url.path == "/run"
                async with httpx.AsyncClient() as model_client:
                    model_response = await model_client.post(
                        f"{local_gateway}/v1/chat/completions",
                        json={"model": "canary", "messages": []},
                    )
                token = model_response.json()["choices"][0]["message"]["content"]
                return httpx.Response(
                    200,
                    json={
                        "final_text": token,
                        "tool_calls": [],
                        "prompt_tokens": 1,
                        "output_tokens": 1,
                        "latency_ms": 1,
                    },
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            gate = BuildGate(make_config(), client)
            async with client:
                result = await gate._run_model_canary(
                    "http://127.0.0.1:8080",
                    token=canary.token,
                    model_called_path=state_file,
                )
            assert result.passed, result.detail
            assert canary.model_calls == 1


async def test_model_canary_http_failure_is_retryable_and_keeps_body(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    gate = _gate_with(make_config(), _ok_run(), tar=_valid_tar())
    del gate._run_model_canary  # type: ignore[attr-defined]

    async def _http_500(*_: Any, **__: Any) -> tuple[int, str]:
        return 22, 'HTTP 500: {"error":"provider response was incompatible"}'

    gate._request_from_sidecar = _http_500  # type: ignore[method-assign]
    result = await gate._run_model_canary(
        "http://harness:8080",
        token="hidden",
        model_called_path=None,
        probe_container="canary",
    )
    await gate._client.aclose()

    assert not result.passed
    assert result.retryable
    assert "HTTP 500" in result.detail
    assert "provider response was incompatible" in result.detail


async def test_failure_diagnostics_include_bounded_container_logs(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    gate = _gate_with(make_config(), _ok_run(), tar=_valid_tar())

    async def _logs(args: list[str], **_: Any) -> tuple[int, str]:
        if args == ["logs", "harness"]:
            return 0, "harness error body"
        if args == ["logs", "canary"]:
            return 0, "gateway request log"
        return 0, ""

    gate._run = _logs  # type: ignore[method-assign]
    detail = await gate._with_container_logs(
        "model canary /run failed: HTTP 500",
        harness_container="harness",
        canary_container="canary",
    )
    await gate._client.aclose()

    assert "harness error body" in detail
    assert "gateway request log" in detail


async def test_sha_mismatch_fails_before_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _valid_tar()
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
    assert not res.passed
    assert res.detail == "contract failed: no Dockerfile at tarball root"


async def test_build_failure_reported(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _valid_tar()
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
    tar = _valid_tar()
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
    tar = _valid_tar()
    sha = hashlib.sha256(tar).hexdigest()
    # run_timeout small so the probe loop exits quickly; the in-network probe fails.
    cfg = make_config(run_timeout_seconds=0.05)

    async def _run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build" and stdin is not None:
            stdin.read()
        if args[0] == "exec" and any("http://harness:" in arg for arg in args):
            return (1, "HTTP Error 503: Service Unavailable")
        return (0, "")

    gate = _gate_with(cfg, _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed and "never healthy" in res.detail


async def test_python_only_solver_fails_contract_before_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tar = _make_tar(
        {
            "Dockerfile": b'FROM python:3.12\nCMD ["python", "miner.py"]\n',
            "miner.py": b"print('benchmark solver')\n",
        }
    )
    sha = hashlib.sha256(tar).hexdigest()
    calls: list[str] = []

    async def _run(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args[0])
        return (0, "")

    gate = _gate_with(make_config(), _run, tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert not res.passed and "Cargo.toml" in res.detail
    assert "build" not in calls


async def test_independent_rust_implementation_is_allowed(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """Miners may fork or replace ditto-harness; dependency choice is not policy."""
    tar = _valid_tar()
    sha = hashlib.sha256(tar).hexdigest()
    gate = _gate_with(make_config(), _ok_run(), tar=tar)
    async with gate._client:
        res = await gate.screen(agent_id=_AGENT, sha256=sha, download_url=_URL)
    assert res.passed

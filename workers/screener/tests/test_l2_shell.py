"""The active signed worker offers source navigation in its isolated analyzer."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest

from ditto_screener import l2_review
from ditto_screener.l2_review import (
    InProcessAnalyzerHarness,
    IsolatedCodingHarness,
    L2LeaseBudgetExhausted,
    _l2_tools_for_policy,
)


def test_shell_is_offered_only_with_isolated_harness() -> None:
    isolated = IsolatedCodingHarness(docker_bin="docker", image="analyzer:test")
    assert isolated.supports_shell
    assert not getattr(InProcessAnalyzerHarness(), "supports_shell", False)
    assert "shell" in {
        tool["name"] for tool in _l2_tools_for_policy(13, shell_enabled=True)
    }
    assert "shell" not in {tool["name"] for tool in _l2_tools_for_policy(13)}


async def test_shell_container_has_read_only_source_and_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _Input:
        def close(self) -> None:
            pass

    class _Process:
        stdin = _Input()
        stdout = object()
        stderr = object()
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def start(*args: str, **kwargs: object) -> _Process:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return _Process()

    async def read(_proc: object, _stream: object, limit: int, out: bytearray) -> bool:
        if limit > 4_096:
            out.extend(b"found\n")
        return False

    monkeypatch.setattr(l2_review.asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(l2_review, "_read_bounded_stream", read)
    output = await IsolatedCodingHarness(
        docker_bin="docker", image="analyzer:test"
    ).run(tmp_path, "shell", {"script": "rg -n model ."})
    assert json.loads(output)["stdout"] == "found\n"
    args = captured["args"]
    assert isinstance(args, tuple)
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--workdir") + 1] == "/workspace"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace,readonly" in args
    assert captured["env"] == {
        "PATH": l2_review.os.environ.get("PATH", "/usr/bin:/bin")
    }


def _fake_docker(tmp_path: Path, run: str) -> tuple[str, Path]:
    """A docker CLI stand-in that logs argv, accepts ``rm``, and ``exec``s ``run``."""
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        '[ "$1" = rm ] && exit 0\n'
        f"exec {run}\n"
    )
    docker.chmod(0o755)
    return str(docker), log


def _removed_container(log: Path, command: str) -> None:
    """Assert the one ``docker run`` was named and then reaped by that name."""
    run, *rest = log.read_text().splitlines()
    argv = run.split()
    name = argv[argv.index("--name") + 1]
    assert name.startswith(f"ditto-l2-{command}-")
    assert rest == [f"rm -f {name}"]


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


async def test_shell_output_over_bound_returns_truncated_observation(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path, "yes found")
    output = await IsolatedCodingHarness(docker_bin=docker, image="analyzer:test").run(
        _workspace(tmp_path), "shell", {"script": "rg -n model ."}
    )

    result = json.loads(output)
    assert result["error"] == "shell-output-bounded"
    assert result["truncated"] is True
    assert result["exit_code"] is None
    assert len(result["stdout"]) == 64_000
    _removed_container(log, "shell")


async def test_shell_stderr_over_bound_returns_truncated_observation(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path, "yes noisy 1>&2")
    output = await IsolatedCodingHarness(docker_bin=docker, image="analyzer:test").run(
        _workspace(tmp_path), "shell", {"script": "rg -n model ."}
    )

    result = json.loads(output)
    assert result["error"] == "shell-output-bounded"
    assert result["truncated"] is True
    assert len(result["stderr"]) == 4_096
    _removed_container(log, "shell")


async def test_shell_timeout_returns_observation_and_removes_container(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path, "sleep 30")
    output = await IsolatedCodingHarness(
        docker_bin=docker, image="analyzer:test", timeout_seconds=0.2
    ).run(_workspace(tmp_path), "shell", {"script": "sleep 60"})

    result = json.loads(output)
    assert result["error"] == "shell-timeout"
    assert result["truncated"] is True
    assert result["exit_code"] is None
    _removed_container(log, "shell")


async def test_shell_deadline_exhaustion_raises_lease_budget_error(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path, "sleep 30")
    harness = IsolatedCodingHarness(docker_bin=docker, image="analyzer:test")
    workspace = _workspace(tmp_path)
    deadline = asyncio.get_running_loop().time() + 0.2

    with pytest.raises(L2LeaseBudgetExhausted, match="lease budget"):
        await harness.run(workspace, "shell", {"script": "sleep 60"}, deadline=deadline)
    _removed_container(log, "shell")
    # A spent lease is refused before any container starts.
    with pytest.raises(L2LeaseBudgetExhausted, match="lease budget"):
        await harness.run(workspace, "shell", {"script": "true"}, deadline=deadline)
    _removed_container(log, "shell")


async def test_non_shell_analyzer_timeout_names_and_removes_container(
    tmp_path: Path,
) -> None:
    docker, log = _fake_docker(tmp_path, "sleep 30")
    output = await IsolatedCodingHarness(
        docker_bin=docker, image="analyzer:test", timeout_seconds=0.2
    ).run(_workspace(tmp_path), "read_file", {"path": "src/main.rs"})

    assert json.loads(output) == {"error": "analyzer-timeout", "truncated": True}
    _removed_container(log, "read-file")


async def test_non_shell_analyzer_output_over_budget_returns_observation(
    tmp_path: Path,
) -> None:
    docker, _log = _fake_docker(tmp_path, "head -c 300000 /dev/zero")
    output = await IsolatedCodingHarness(docker_bin=docker, image="analyzer:test").run(
        _workspace(tmp_path), "search", {"query": "model"}
    )

    assert json.loads(output) == {
        "error": "analyzer-output-truncated",
        "truncated": True,
    }


async def test_non_shell_analyzer_cancel_removes_container(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path, "sleep 30")
    task = asyncio.create_task(
        IsolatedCodingHarness(docker_bin=docker, image="analyzer:test").run(
            _workspace(tmp_path), "search", {"query": "model"}
        )
    )
    while not log.exists():
        await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    _removed_container(log, "search")


@pytest.mark.parametrize(
    ("run", "expected"),
    [("sleep 30", "analyzer-timeout"), ("head -c 300000 /dev/zero", None)],
)
async def test_local_harness_returns_bounded_observations(
    tmp_path: Path, run: str, expected: str | None
) -> None:
    python, _log = _fake_docker(tmp_path, run)
    output = await InProcessAnalyzerHarness(python_bin=python, timeout_seconds=0.2).run(
        _workspace(tmp_path), "search", {"query": "model"}
    )

    assert json.loads(output) == {
        "error": expected or "analyzer-output-truncated",
        "truncated": True,
    }

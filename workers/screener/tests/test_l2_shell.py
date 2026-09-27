"""The active signed worker offers source navigation in its isolated analyzer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto_screener import l2_review
from ditto_screener.l2_review import (
    InProcessAnalyzerHarness,
    IsolatedCodingHarness,
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

    async def read(_stream: object, limit: int) -> bytes:
        return b"found\n" if limit > 4_096 else b""

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

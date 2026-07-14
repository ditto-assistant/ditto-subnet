"""Tests for the local validator update/drain coordination contract."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.validator import worker as worker_mod
from ditto.validator.update_control import (
    bootstrap_should_start_drained,
    mark_bootstrap_resumed,
    write_update_state,
)
from ditto.validator.worker import ValidatorWorker


def test_update_state_is_atomic_bounded_and_private(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    write_update_state("drained", path=path)

    payload = json.loads(path.read_text())
    assert payload == {
        "compatibility_epoch": 1,
        "heartbeat_protocol": 4,
        "pid": payload["pid"],
        "platform_accepted": False,
        "state": "drained",
        "update_protocol": 1,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_bootstrap_resume_marker_survives_process_restart(tmp_path: Path) -> None:
    marker = tmp_path / "resumed"

    assert bootstrap_should_start_drained(True, marker_path=marker)
    assert mark_bootstrap_resumed(marker_path=marker)

    assert not bootstrap_should_start_drained(True, marker_path=marker)
    assert not bootstrap_should_start_drained(False, marker_path=tmp_path / "missing")
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


async def test_run_forever_acknowledges_only_quiescent_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[str] = []
    monkeypatch.setattr(
        worker_mod,
        "write_update_state",
        lambda state, **_kwargs: states.append(state),
    )
    config = MagicMock(epoch_seconds=3600, sweep_seconds=120)
    chain = MagicMock()
    chain.get_weights_rate_limit = AsyncMock(return_value=None)
    worker = ValidatorWorker(
        config=config,
        platform=MagicMock(),
        dittobench=MagicMock(),
        chain=chain,
        keypair=MagicMock(),
    )
    run_once = AsyncMock()
    monkeypatch.setattr(worker, "run_once", run_once)
    stop = asyncio.Event()
    drain = asyncio.Event()
    drain.set()

    task = asyncio.create_task(worker.run_forever(stop, drain_requested=drain))
    for _ in range(20):
        if "drained" in states:
            break
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert states[:2] == ["ready", "drained"]
    assert states[-1] == "stopping"
    run_once.assert_not_awaited()

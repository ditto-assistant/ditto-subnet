"""Tests for the local validator update/drain coordination contract."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.validator import worker as worker_mod
from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION
from ditto.validator.update_control import (
    bootstrap_should_start_drained,
    mark_bootstrap_resumed,
    request_fleet_update,
    write_update_state,
)
from ditto.validator.worker import ValidatorWorker


def test_update_state_is_atomic_bounded_and_private(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    write_update_state("drained", path=path)

    payload = json.loads(path.read_text())
    assert payload == {
        "compatibility_epoch": 2,
        "heartbeat_protocol": HEARTBEAT_PROTOCOL_VERSION,
        "pid": payload["pid"],
        "platform_accepted": False,
        "state": "drained",
        "update_protocol": 1,
        "fleet_update_operation_id": None,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_fleet_update_id_is_visible_to_the_host_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id = uuid4()
    path = tmp_path / "state.json"
    monkeypatch.setattr(
        "ditto.validator.update_control._fleet_update_operation_id", None
    )

    request_fleet_update(operation_id)
    write_update_state("drained", platform_accepted=True, path=path)

    payload = json.loads(path.read_text())
    assert payload["fleet_update_operation_id"] == str(operation_id)
    assert payload["platform_accepted"] is True


def test_bootstrap_resume_marker_survives_process_restart(tmp_path: Path) -> None:
    marker = tmp_path / "resumed"

    assert bootstrap_should_start_drained(True, marker_path=marker)
    assert mark_bootstrap_resumed(marker_path=marker)

    assert not bootstrap_should_start_drained(True, marker_path=marker)
    assert not bootstrap_should_start_drained(False, marker_path=tmp_path / "missing")
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_bootstrap_resume_marker_prunes_prior_deployment_tokens(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous.resumed"
    current = tmp_path / "current.resumed"
    previous.write_text("resumed\n")

    assert mark_bootstrap_resumed(marker_path=current)

    assert current.read_text() == "resumed\n"
    assert not previous.exists()


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


async def test_resuming_forced_noop_update_reopens_confirmation_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[str] = []
    monkeypatch.setattr(
        worker_mod,
        "write_update_state",
        lambda state, **_kwargs: states.append(state),
    )
    worker = ValidatorWorker(
        config=MagicMock(),
        platform=MagicMock(),
        dittobench=MagicMock(),
        chain=MagicMock(),
        keypair=MagicMock(),
    )
    monkeypatch.setattr(worker, "_report_heartbeat", AsyncMock(return_value=True))
    worker._force_cancel_requested.set()
    stop = asyncio.Event()
    drain = asyncio.Event()
    drain.set()

    task = asyncio.create_task(worker._acknowledge_drain(stop, drain))
    for _ in range(20):
        if "drained" in states:
            break
        await asyncio.sleep(0)
    drain.clear()
    await asyncio.wait_for(task, timeout=1)

    assert not worker._force_cancel_requested.is_set()
    assert states == ["drained", "ready"]

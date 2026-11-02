"""Observe single-flight failures without dispatching or leaking private errors."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.api_server.ledger_pin import LedgerPinLoop, LedgerPinMaterializer
from ditto.chain.errors import ChainTreasuryReadTimeoutError
from ditto.tests.api_server.test_ledger_pin import _NOW, _schedule


async def test_failure_is_bounded_and_next_success_is_not_cached(monkeypatch):
    producer = LedgerPinMaterializer()

    async def broken(*_args, **_kwargs):
        producer._stage("treasury_observation")
        raise ValueError("secret-token SQL private-provider-url")

    monkeypatch.setattr(producer, "_load_or_build", broken)
    assert await producer.ensure(None, None, now=_NOW, schedule=_schedule()) is None
    failure = producer.diagnostic()
    assert failure.stage == "treasury_observation"
    assert failure.outcome == "error"
    assert failure.failure.kind == "value"
    assert failure.epoch_index == _schedule().subnet_epoch_index
    assert not failure.in_progress
    assert failure.elapsed_seconds >= 0
    assert failure.finished_at >= failure.started_at
    assert "secret-token" not in failure.model_dump_json()
    assert "test_ledger_pin_diagnostic" not in failure.model_dump_json()
    assert producer.newest_known is None

    monkeypatch.setattr(
        producer,
        "_load_or_build",
        AsyncMock(
            return_value=SimpleNamespace(epoch_index=_schedule().subnet_epoch_index)
        ),
    )
    assert await producer.ensure(None, None, now=_NOW, schedule=_schedule()) is not None
    recovered = producer.diagnostic()
    assert recovered.outcome == "pinned"
    assert recovered.failure is None
    assert recovered.last_success_epoch == _schedule().subnet_epoch_index


async def test_in_flight_snapshot_and_cancellation_release_lock(monkeypatch):
    producer = LedgerPinMaterializer()
    entered = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        producer._stage("ledger_snapshot")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(producer, "_load_or_build", blocked)
    task = asyncio.create_task(
        producer.ensure(None, None, now=_NOW, schedule=_schedule())
    )
    await entered.wait()
    snapshot = producer.diagnostic()
    assert snapshot.in_progress
    assert snapshot.stage == "ledger_snapshot"
    assert snapshot.elapsed_seconds >= 0
    assert producer.diagnostic().elapsed_seconds >= snapshot.elapsed_seconds
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert producer.diagnostic().outcome == "cancelled"
    assert not producer.diagnostic().in_progress
    assert not producer._lock.locked()


async def test_loop_records_pre_materializer_failure():
    producer = LedgerPinMaterializer()
    producer.ensure = AsyncMock()
    state = SimpleNamespace(
        continual_retest_settings=SimpleNamespace(
            resolve=AsyncMock(side_effect=RuntimeError("secret settings row"))
        )
    )
    loop = LedgerPinLoop(app_state=state, session_maker=None, materializer=producer)
    with pytest.raises(RuntimeError):
        await loop.sweep()
    result = loop.diagnostic()
    assert result.task_state == "not_started"
    assert result.stage == "settings"
    assert result.failure.kind == "runtime"
    assert result.finished_at is not None
    assert not result.in_progress
    assert "secret settings row" not in result.model_dump_json()
    producer.ensure.assert_not_awaited()


async def test_loop_exposes_stalled_settings_read():
    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()

    state = SimpleNamespace(continual_retest_settings=SimpleNamespace(resolve=blocked))
    loop = LedgerPinLoop(
        app_state=state, session_maker=None, materializer=LedgerPinMaterializer()
    )
    await loop.start()
    await entered.wait()
    assert loop.diagnostic().task_state == "running"
    assert loop.diagnostic().in_progress
    assert loop.diagnostic().stage == "settings"
    loop._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop._task
    assert loop.diagnostic().task_state == "cancelled"
    assert not loop.diagnostic().in_progress


async def test_chain_timeout_reports_fixed_checkpoint(monkeypatch):
    producer = LedgerPinMaterializer()
    monkeypatch.setattr(
        producer,
        "_load_or_build",
        AsyncMock(side_effect=ChainTreasuryReadTimeoutError("collector_storage")),
    )
    assert await producer.ensure(None, None, now=_NOW, schedule=_schedule()) is None
    assert producer.diagnostic().failure.kind == "timeout"
    assert producer.diagnostic().failure.read_step == "collector_storage"

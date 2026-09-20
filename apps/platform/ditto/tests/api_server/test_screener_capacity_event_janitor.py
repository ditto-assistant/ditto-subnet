"""Retention janitor behaviour for screener capacity audit events."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server import screener_capacity_event_janitor as janitor_module
from ditto.api_server.screener_capacity_event_janitor import (
    ScreenerCapacityEventJanitor,
)
from ditto.db.models import ScreenerCapacityEvent
from ditto.db.queries.screener_capacity_events import (
    SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY,
)
from ditto.metrics import (
    SCREENER_CAPACITY_EVENT_JANITOR_DELETED,
    SCREENER_CAPACITY_EVENT_JANITOR_RUNS,
)

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def _seed(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    old: int,
    recent: int,
    environment: str = "prod",
) -> None:
    async with session_maker() as session, session.begin():
        for ordinal in range(old):
            session.add(_event(_NOW - timedelta(days=31, minutes=ordinal), environment))
        for ordinal in range(recent):
            session.add(_event(_NOW - timedelta(days=1, minutes=ordinal), environment))


def _event(created_at: datetime, environment: str = "prod") -> ScreenerCapacityEvent:
    return ScreenerCapacityEvent(
        event_id=uuid4(),
        environment=environment,
        event_type="reconcile",
        detail="test",
        controller_epoch="epoch-1",
        created_at=created_at,
    )


async def _count(session_maker: async_sessionmaker[AsyncSession]) -> int:
    async with session_maker() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(ScreenerCapacityEvent)
            )
            or 0
        )


def _deleted() -> float:
    return SCREENER_CAPACITY_EVENT_JANITOR_DELETED._value.get()


def _runs(outcome: str) -> float:
    return SCREENER_CAPACITY_EVENT_JANITOR_RUNS.labels(outcome=outcome)._value.get()


async def test_sweep_drains_multiple_batches_and_keeps_recent_events(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_maker, old=5, recent=2)
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, batch_size=2
    )
    assert await janitor.sweep(now=_NOW) == 5
    assert await _count(session_maker) == 2


async def test_sweep_stops_at_max_batches(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_maker, old=5, recent=0)
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, batch_size=2, max_batches=2
    )
    assert await janitor.sweep(now=_NOW) == 4
    assert await _count(session_maker) == 1


async def test_zero_retention_keeps_every_event_and_never_starts(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_maker, old=3, recent=0)
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=0
    )
    assert await janitor.sweep(now=_NOW) == 0
    await janitor.start()
    await janitor.aclose()
    assert await _count(session_maker) == 3


async def test_failed_sweep_is_counted_and_raised() -> None:
    def broken() -> AsyncSession:
        raise RuntimeError("database unavailable")

    janitor = ScreenerCapacityEventJanitor(
        session_maker=broken,  # type: ignore[arg-type]
        retention_days=30,
    )
    before = _runs("error")
    with pytest.raises(RuntimeError):
        await janitor.sweep(now=_NOW)
    assert _runs("error") == before + 1


def test_rejects_invalid_settings(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    for kwargs in (
        {"retention_days": -1},
        {"retention_days": 30, "interval_seconds": 0},
        {"retention_days": 30, "batch_size": 0},
        {"retention_days": 30, "max_batches": 0},
    ):
        with pytest.raises(ValueError):
            ScreenerCapacityEventJanitor(session_maker=session_maker, **kwargs)


def _fail_or_busy_on_call(
    monkeypatch: pytest.MonkeyPatch, *, call: int, outcome: object
) -> None:
    """Run the real delete, but replace the ``call``-th batch with ``outcome``."""
    real = janitor_module.delete_expired_screener_capacity_events
    calls = 0

    async def wrapper(session: AsyncSession, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == call:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return await real(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        janitor_module, "delete_expired_screener_capacity_events", wrapper
    )


async def test_busy_batch_keeps_the_rows_already_deleted_in_the_metric(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(session_maker, old=9, recent=0)
    _fail_or_busy_on_call(monkeypatch, call=3, outcome=None)
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, batch_size=2
    )
    deleted_before, busy_before = _deleted(), _runs("busy")

    # Batches 1 and 2 delete four rows before batch 3 finds the lock taken.
    assert await janitor.sweep(now=_NOW) == 4
    assert _deleted() == deleted_before + 4
    assert _runs("busy") == busy_before + 1
    assert await _count(session_maker) == 5


async def test_failed_batch_keeps_the_rows_already_deleted_in_the_metric(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(session_maker, old=9, recent=0)
    _fail_or_busy_on_call(monkeypatch, call=3, outcome=RuntimeError("boom"))
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, batch_size=2
    )
    deleted_before, error_before = _deleted(), _runs("error")

    with pytest.raises(RuntimeError, match="boom"):
        await janitor.sweep(now=_NOW)
    assert _deleted() == deleted_before + 4
    assert _runs("error") == error_before + 1


async def test_busy_sweep_through_the_janitor_deletes_nothing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_maker, old=3, recent=0)
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30
    )
    busy_before, deleted_before = _runs("busy"), _deleted()
    async with session_maker() as holder:
        await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY},
        )
        assert await janitor.sweep(now=_NOW) == 0
        await holder.rollback()
    assert _runs("busy") == busy_before + 1
    assert _deleted() == deleted_before
    assert await _count(session_maker) == 3


async def test_environments_are_listed_once_per_sweep(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(session_maker, old=7, recent=0)
    real = janitor_module.list_screener_capacity_event_environments
    listed = 0

    async def counting(session: AsyncSession) -> list[str]:
        nonlocal listed
        listed += 1
        return await real(session)

    monkeypatch.setattr(
        janitor_module, "list_screener_capacity_event_environments", counting
    )
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, batch_size=2
    )
    assert await janitor.sweep(now=_NOW) == 7  # four batches, one listing
    assert listed == 1


async def test_deep_backlog_in_one_environment_does_not_starve_another(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(session_maker, old=9, recent=1, environment="dev")
    await _seed(session_maker, old=2, recent=1, environment="prod")
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker,
        retention_days=30,
        batch_size=2,
        max_batches=2,
    )
    # Batch 1 takes 2 from each; prod is then drained and drops out, so batch 2
    # only touches dev. The old shared budget would have spent both on dev.
    assert await janitor.sweep(now=_NOW) == 6
    async with session_maker() as session:
        remaining = dict(
            (
                await session.execute(
                    select(ScreenerCapacityEvent.environment, func.count())
                    .group_by(ScreenerCapacityEvent.environment)
                    .order_by(ScreenerCapacityEvent.environment)
                )
            ).all()
        )
    assert remaining == {"dev": 6, "prod": 1}


async def test_loop_survives_a_failed_sweep_and_stops_on_aclose(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    janitor = ScreenerCapacityEventJanitor(
        session_maker=session_maker, retention_days=30, interval_seconds=0.01
    )
    sweeps = 0
    recovered = asyncio.Event()

    async def flaky_sweep(**_kwargs: object) -> int:
        nonlocal sweeps
        sweeps += 1
        if sweeps == 1:
            raise RuntimeError("transient database failure")
        if sweeps >= 3:
            recovered.set()
        return 0

    monkeypatch.setattr(janitor, "sweep", flaky_sweep)
    await janitor.start()
    await asyncio.wait_for(recovered.wait(), timeout=5)  # kept going after the raise
    await janitor.aclose()

    settled = sweeps
    await asyncio.sleep(0.1)
    assert sweeps == settled  # no sweeps after aclose
    assert janitor._task is None

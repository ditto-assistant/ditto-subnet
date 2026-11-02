"""Real-Postgres retention tests for screener capacity audit events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import ScreenerCapacityEvent
from ditto.db.queries.screener_capacity_events import (
    SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY,
    acquire_screener_capacity_event_janitor_lock,
    delete_expired_screener_capacity_events,
    list_screener_capacity_event_environments,
)

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_CUTOFF = _NOW - timedelta(days=30)


def _event(environment: str, created_at: datetime) -> ScreenerCapacityEvent:
    return ScreenerCapacityEvent(
        event_id=uuid4(),
        environment=environment,
        event_type="reconcile",
        detail="test",
        controller_epoch="epoch-1",
        created_at=created_at,
    )


async def _count(
    session_maker: async_sessionmaker[AsyncSession], environment: str | None = None
) -> int:
    async with session_maker() as session:
        query = select(func.count()).select_from(ScreenerCapacityEvent)
        if environment is not None:
            query = query.where(ScreenerCapacityEvent.environment == environment)
        return int(await session.scalar(query) or 0)


async def test_row_exactly_at_the_cutoff_is_kept(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                _event("prod", _CUTOFF - timedelta(seconds=1)),
                _event("prod", _CUTOFF),
                _event("prod", _NOW),
            ]
        )
    async with session_maker() as session, session.begin():
        assert await delete_expired_screener_capacity_events(
            session, environments=["prod"], before=_CUTOFF, limit=100
        ) == {"prod": 1}
    assert await _count(session_maker) == 2


async def test_pruning_is_bounded_and_keeps_recent_history(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        session.add_all(
            [_event("prod", _CUTOFF - timedelta(days=n + 1)) for n in range(5)]
            + [_event("prod", _NOW - timedelta(days=1))]
        )
    async with session_maker() as session, session.begin():
        assert await delete_expired_screener_capacity_events(
            session, environments=["prod"], before=_CUTOFF, limit=2
        ) == {"prod": 2}
    assert await _count(session_maker) == 4
    async with session_maker() as session, session.begin():
        assert await delete_expired_screener_capacity_events(
            session, environments=["prod"], before=_CUTOFF, limit=100
        ) == {"prod": 3}
    assert await _count(session_maker) == 1


async def test_each_environment_gets_its_own_budget(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    old = _CUTOFF - timedelta(days=1)
    async with session_maker() as session, session.begin():
        session.add_all(
            [_event("dev", old) for _ in range(9)]
            + [_event("prod", old) for _ in range(3)]
        )
    async with session_maker() as session, session.begin():
        # A deep ``dev`` backlog must not use up the budget ``prod`` needs.
        assert await delete_expired_screener_capacity_events(
            session, environments=["dev", "prod"], before=_CUTOFF, limit=2
        ) == {"dev": 2, "prod": 2}
    assert await _count(session_maker, "dev") == 7
    assert await _count(session_maker, "prod") == 1


async def test_only_expired_rows_of_each_environment_are_removed(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    old = _CUTOFF - timedelta(days=1)
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                _event("prod", old),
                _event("prod", _NOW),
                _event("dev", old),
                _event("dev", _NOW),
                _event("staging", _NOW),
            ]
        )
    async with session_maker() as session, session.begin():
        assert await delete_expired_screener_capacity_events(
            session,
            environments=["prod", "dev", "staging"],
            before=_CUTOFF,
            limit=100,
        ) == {"prod": 1, "dev": 1, "staging": 0}
    assert await _count(session_maker, "prod") == 1
    assert await _count(session_maker, "dev") == 1
    assert await _count(session_maker, "staging") == 1


async def test_lock_is_refused_while_another_transaction_holds_it(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as holder, holder.begin():
        assert await acquire_screener_capacity_event_janitor_lock(holder) is True
        async with session_maker() as contender, contender.begin():
            # try-lock: refused immediately, never waits.
            assert (
                await acquire_screener_capacity_event_janitor_lock(contender) is False
            )
    async with session_maker() as later, later.begin():
        # The lock is transaction scoped, so it went away with the holder's.
        assert await acquire_screener_capacity_event_janitor_lock(later) is True


async def test_lock_is_still_taken_when_the_key_is_held_by_a_raw_advisory_lock(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The lock key is the public contract other replicas contend on."""
    async with session_maker() as holder, holder.begin():
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY},
        )
        async with session_maker() as contender, contender.begin():
            assert (
                await acquire_screener_capacity_event_janitor_lock(contender) is False
            )


async def test_environment_with_no_expired_rows_is_left_alone(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        session.add_all([_event("prod", _NOW), _event("dev", _NOW)])
    async with session_maker() as session, session.begin():
        assert await delete_expired_screener_capacity_events(
            session, environments=["dev", "prod"], before=_CUTOFF, limit=100
        ) == {"dev": 0, "prod": 0}
    assert await _count(session_maker) == 2


async def test_lists_each_environment_once_in_a_stable_order(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session:
        assert await list_screener_capacity_event_environments(session) == []
    async with session_maker() as session, session.begin():
        session.add_all(
            [_event("prod", _NOW), _event("prod", _CUTOFF), _event("dev", _NOW)]
        )
    async with session_maker() as session:
        assert await list_screener_capacity_event_environments(session) == [
            "dev",
            "prod",
        ]

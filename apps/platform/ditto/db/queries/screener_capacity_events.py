"""Bounded retention for the append-only screener capacity audit table."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY = "ditto:screener-capacity-event-janitor:v1"


async def list_screener_capacity_event_environments(
    session: AsyncSession,
) -> list[str]:
    """Return every environment holding events, in a stable order.

    PostgreSQL has no loose index scan before version 18, so this reads the
    whole ``(environment, created_at)`` index. Callers fetch it once per sweep
    rather than once per batch.
    """
    result = await session.scalars(
        text("SELECT DISTINCT environment FROM screener_capacity_events")
    )
    return sorted(result)


async def acquire_screener_capacity_event_janitor_lock(session: AsyncSession) -> bool:
    """Try to take the sweep lock for the rest of the current transaction.

    The lock is transaction scoped, so it is held exactly as long as the caller
    keeps one transaction open. Run the whole bounded sweep inside it: taking it
    per batch would let another replica start its own sweep between batches and
    exceed the documented deletion budget. Returns ``False`` when another
    replica holds it.
    """
    if session.get_bind().dialect.name != "postgresql":
        # SQLite-backed unit tests do not exercise the production janitor.
        return True
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY},
    )
    return bool(locked)


async def delete_expired_screener_capacity_events(
    session: AsyncSession,
    *,
    environments: Sequence[str],
    before: datetime,
    limit: int,
) -> dict[str, int]:
    """Delete up to ``limit`` events older than ``before`` in *each* environment.

    Every environment gets its own budget, so a backlog in one cannot starve
    another, and each environment is pruned against its own rows, which lets
    the ``(environment, created_at)`` index serve the scan. A row exactly at
    ``before`` is kept. Returns the per-environment delete counts.

    The caller must already hold :func:`acquire_screener_capacity_event_janitor_lock`.
    """
    if limit <= 0:
        raise ValueError("screener capacity event janitor limit must be positive")
    if session.get_bind().dialect.name != "postgresql":
        # SQLite-backed unit tests do not exercise the production janitor;
        # keeping the SQL PostgreSQL-only stops a test-only query drifting.
        return dict.fromkeys(environments, 0)
    deleted: dict[str, int] = {}
    for environment in environments:
        result = await session.execute(
            text(
                """
                WITH expired AS (
                    SELECT event_id
                    FROM screener_capacity_events
                    WHERE environment = :environment AND created_at < :before
                    ORDER BY created_at, event_id
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                )
                DELETE FROM screener_capacity_events AS event
                USING expired
                WHERE event.event_id = expired.event_id
                RETURNING event.event_id
                """
            ),
            {"environment": environment, "before": before, "limit": limit},
        )
        deleted[environment] = len(result.all())
    return deleted


__all__ = [
    "SCREENER_CAPACITY_EVENT_JANITOR_LOCK_KEY",
    "acquire_screener_capacity_event_janitor_lock",
    "delete_expired_screener_capacity_events",
    "list_screener_capacity_event_environments",
]

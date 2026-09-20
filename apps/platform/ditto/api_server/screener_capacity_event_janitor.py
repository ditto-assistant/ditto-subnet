"""Bounded out-of-band retention for screener capacity audit events."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING

from ditto.db.queries.screener_capacity_events import (
    delete_expired_screener_capacity_events,
    list_screener_capacity_event_environments,
)
from ditto.metrics import (
    SCREENER_CAPACITY_EVENT_JANITOR_DELETED,
    SCREENER_CAPACITY_EVENT_JANITOR_DURATION_SECONDS,
    SCREENER_CAPACITY_EVENT_JANITOR_RUNS,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


logger = logging.getLogger(__name__)

DEFAULT_CAPACITY_EVENT_JANITOR_INTERVAL_SECONDS = 3_600.0
DEFAULT_CAPACITY_EVENT_JANITOR_BATCH_SIZE = 1_000
DEFAULT_CAPACITY_EVENT_JANITOR_MAX_BATCHES = 20


class ScreenerCapacityEventJanitor:
    """Periodically prune expired capacity events in short bounded batches.

    ``retention_days == 0`` keeps every event: the loop never starts and
    ``sweep`` deletes nothing.
    """

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        retention_days: int,
        interval_seconds: float = DEFAULT_CAPACITY_EVENT_JANITOR_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_CAPACITY_EVENT_JANITOR_BATCH_SIZE,
        max_batches: int = DEFAULT_CAPACITY_EVENT_JANITOR_MAX_BATCHES,
    ) -> None:
        if retention_days < 0:
            raise ValueError("capacity event retention days cannot be negative")
        if interval_seconds <= 0:
            raise ValueError("capacity event janitor interval must be positive")
        if batch_size <= 0:
            raise ValueError("capacity event janitor batch size must be positive")
        if max_batches <= 0:
            raise ValueError("capacity event janitor max batches must be positive")
        self._session_maker = session_maker
        self._retention_days = retention_days
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._max_batches = max_batches
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def retention_days(self) -> int:
        return self._retention_days

    async def start(self) -> None:
        if self._task is not None or self._retention_days == 0:
            return
        self._task = asyncio.create_task(
            self._run(), name="screener-capacity-event-janitor"
        )

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def sweep(self, *, now: datetime | None = None) -> int:
        """Run up to ``max_batches`` transactions; exposed for real-DB tests.

        Environments are listed once, then each batch prunes at most
        ``batch_size`` rows per environment. An environment drops out of later
        batches as soon as one comes back short. The deleted counter moves per
        batch, so a busy or failed later batch never hides earlier deletes.
        """
        if self._retention_days == 0:
            return 0
        before = (now or datetime.now(UTC)) - timedelta(days=self._retention_days)
        started = monotonic()
        total = 0
        try:
            async with self._session_maker() as session, session.begin():
                active = await list_screener_capacity_event_environments(session)
            for _ in range(self._max_batches):
                if not active:
                    break
                async with self._session_maker() as session, session.begin():
                    deleted = await delete_expired_screener_capacity_events(
                        session,
                        environments=active,
                        before=before,
                        limit=self._batch_size,
                    )
                if deleted is None:
                    SCREENER_CAPACITY_EVENT_JANITOR_RUNS.labels(outcome="busy").inc()
                    return total
                batch_total = sum(deleted.values())
                total += batch_total
                SCREENER_CAPACITY_EVENT_JANITOR_DELETED.inc(batch_total)
                active = [
                    environment
                    for environment in active
                    if deleted[environment] >= self._batch_size
                ]
        except Exception:
            SCREENER_CAPACITY_EVENT_JANITOR_RUNS.labels(outcome="error").inc()
            logger.exception("screener capacity event janitor sweep failed")
            raise
        finally:
            SCREENER_CAPACITY_EVENT_JANITOR_DURATION_SECONDS.observe(
                monotonic() - started
            )
        SCREENER_CAPACITY_EVENT_JANITOR_RUNS.labels(outcome="deleted").inc()
        return total

    async def _run(self) -> None:
        while not self._stop.is_set():
            # Retention is best effort; the audit table only ever grows if a
            # sweep fails, and the failure is already logged and counted.
            with suppress(Exception):
                await self.sweep()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = [
    "DEFAULT_CAPACITY_EVENT_JANITOR_BATCH_SIZE",
    "DEFAULT_CAPACITY_EVENT_JANITOR_INTERVAL_SECONDS",
    "DEFAULT_CAPACITY_EVENT_JANITOR_MAX_BATCHES",
    "ScreenerCapacityEventJanitor",
]

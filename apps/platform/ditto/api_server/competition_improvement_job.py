"""Hourly job that materializes the competition improvement metrics.

Measurement only: every source table is read, and the only writes go to the
three ``competition_*`` measurement tables. The first tick after boot
backfills every day from the first bench version in scope; later ticks
refresh the open days, add the new one and recompute releases and forks.
Closed days are never rewritten under the same metrics version.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from ditto.api_server.competition_improvement import METRICS_VERSION
from ditto.db.queries.competition_improvement import compute, store

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600.0
# Let boot-time work settle before the first (largest) backfill read.
DEFAULT_INITIAL_DELAY_SECONDS = 120.0


class CompetitionImprovementJob:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("competition improvement interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._initial_delay_seconds = max(0.0, initial_delay_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_started: datetime | None = None
        self._last_finished: datetime | None = None
        self._last_counters: dict[str, int] = {}
        self._last_error: str | None = None

    def runtime(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "metrics_version": METRICS_VERSION,
            "interval_seconds": self._interval_seconds,
            "last_tick_started_at": self._last_started,
            "last_tick_finished_at": self._last_finished,
            "last_tick_counters": dict(self._last_counters),
            "last_tick_error": self._last_error,
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="competition-improvement-job"
            )

    async def aclose(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _wait(self, seconds: float) -> bool:
        """Sleep ``seconds``; True when asked to stop meanwhile."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _run(self) -> None:
        if self._initial_delay_seconds and await self._wait(
            self._initial_delay_seconds
        ):
            return
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"[:500]
                self._last_finished = datetime.now(UTC)
                logger.exception("competition improvement tick failed")
            if await self._wait(self._interval_seconds):
                return

    async def tick(self, *, now: datetime | None = None) -> dict[str, int]:
        started = monotonic()
        now = now or datetime.now(UTC)
        self._last_started = datetime.now(UTC)
        self._last_error = None
        async with self._session_maker() as session:
            computation = await compute(session, now=now)
        async with self._session_maker() as session, session.begin():
            counters = await store(session, computation, now=now)
        self._last_counters = {
            **counters,
            "elapsed_ms": int((monotonic() - started) * 1000),
        }
        self._last_finished = datetime.now(UTC)
        return counters

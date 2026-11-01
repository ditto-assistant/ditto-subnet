"""Platform-role loop running the post-outage automatic void sweep.

See :mod:`ditto.db.queries.outage_sweep` for what one sweep does. The loop is
a singleton on the platform role (like the bounty deadline sweeper); the
sweep also takes a transaction-scoped advisory lock, so a second process
would skip rather than double-examine.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.outage_sweep import SWEEP_INTERVAL_SECONDS, sweep_outage_voids

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.db.models import OutageSweepRun

logger = logging.getLogger(__name__)


class OutageVoidSweeper:
    """Every :data:`SWEEP_INTERVAL_SECONDS`, void outage-tainted retest samples."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("outage sweep interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="outage-void-sweeper")

    async def aclose(self, *, grace_seconds: float = 5.0) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        except TimeoutError:
            # The sweep's transaction rolls back; the next start resumes from
            # the same watermark.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def sweep(self, *, now: datetime | None = None) -> OutageSweepRun | None:
        at = now or datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            version = await active_bench_version(session)
            run = await sweep_outage_voids(session, now=at, bench_version=version)
        if run is not None and (run.voided_count or run.canonical_flagged):
            logger.warning(
                "outage sweep voided %d sample(s) and flagged %d canonical "
                "score(s) on v%d",
                run.voided_count,
                len(run.canonical_flagged),
                run.bench_version,
            )
        return run

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:
                # The watermark only advances with a committed run, so a
                # failed sweep is retried in full on the next tick.
                logger.exception("outage void sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = ["OutageVoidSweeper"]

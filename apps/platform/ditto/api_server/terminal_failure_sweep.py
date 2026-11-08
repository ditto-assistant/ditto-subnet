"""Periodic catch-up for terminal artifact failure close-outs.

``POST /validator/job/fail`` closes out a submission the moment its own
failures make scoring quorum unreachable, but it does so after the
validator's report has committed and only logs a failure, so the report is
never lost. That leaves two gaps this loop fills: a close-out the hook dropped
on a transient error, and rows stranded before close-outs existed. Each sweep
re-runs the same fail-closed decision under row locks, so a second replica
running concurrently serializes on the agent row and writes nothing twice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ditto.db.queries.terminal_failure import sweep_terminal_artifact_failures

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_TERMINAL_FAILURE_SWEEP_INTERVAL_SECONDS = 600.0


class TerminalFailureSweeper:
    """Run :func:`sweep_terminal_artifact_failures` on a fixed interval."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        interval_seconds: float = DEFAULT_TERMINAL_FAILURE_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("terminal failure sweep interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="terminal-failure-sweep")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def sweep(self) -> int:
        """One pass; exposed for real-DB tests."""
        written = await sweep_terminal_artifact_failures(self._session_maker)
        if written:
            logger.warning(
                "terminal failure sweep closed out %s submission(s)", written
            )
        return written

    async def _run(self) -> None:
        while not self._stop.is_set():
            # Best effort: a failed pass is logged and the next one retries.
            try:
                await self.sweep()
            except Exception:
                logger.exception("terminal failure sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = [
    "DEFAULT_TERMINAL_FAILURE_SWEEP_INTERVAL_SECONDS",
    "TerminalFailureSweeper",
]

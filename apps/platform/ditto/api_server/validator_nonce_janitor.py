"""Bounded out-of-band cleanup for validator replay guards."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

from ditto.db.queries.validator_auth import delete_expired_validator_nonces
from ditto.metrics import (
    VALIDATOR_NONCE_JANITOR_DELETED,
    VALIDATOR_NONCE_JANITOR_DURATION_SECONDS,
    VALIDATOR_NONCE_JANITOR_RUNS,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


logger = logging.getLogger(__name__)

DEFAULT_VALIDATOR_NONCE_JANITOR_INTERVAL_SECONDS = 60.0
DEFAULT_VALIDATOR_NONCE_JANITOR_BATCH_SIZE = 1_000


class ValidatorNonceJanitor:
    """Periodically prune one bounded batch without joining request traffic."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_VALIDATOR_NONCE_JANITOR_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_VALIDATOR_NONCE_JANITOR_BATCH_SIZE,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("validator nonce janitor interval must be positive")
        if batch_size <= 0:
            raise ValueError("validator nonce janitor batch size must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="validator-nonce-janitor")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def sweep(self, *, now: datetime | None = None) -> int | None:
        """Run one transaction-bounded sweep; exposed for real-DB tests."""
        started = monotonic()
        try:
            async with self._session_maker() as session, session.begin():
                deleted = await delete_expired_validator_nonces(
                    session,
                    now=now or datetime.now(UTC),
                    limit=self._batch_size,
                )
        except Exception:
            VALIDATOR_NONCE_JANITOR_RUNS.labels(outcome="error").inc()
            logger.exception("validator nonce janitor sweep failed")
            raise
        finally:
            VALIDATOR_NONCE_JANITOR_DURATION_SECONDS.observe(monotonic() - started)
        if deleted is None:
            VALIDATOR_NONCE_JANITOR_RUNS.labels(outcome="busy").inc()
        else:
            VALIDATOR_NONCE_JANITOR_RUNS.labels(outcome="deleted").inc()
            VALIDATOR_NONCE_JANITOR_DELETED.inc(deleted)
        return deleted

    async def _run(self) -> None:
        while not self._stop.is_set():
            # Cleanup is best effort. Replay protection remains fail-closed
            # because request transactions only insert unique nonce rows.
            with suppress(Exception):
                await self.sweep()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = [
    "DEFAULT_VALIDATOR_NONCE_JANITOR_BATCH_SIZE",
    "DEFAULT_VALIDATOR_NONCE_JANITOR_INTERVAL_SECONDS",
    "ValidatorNonceJanitor",
]

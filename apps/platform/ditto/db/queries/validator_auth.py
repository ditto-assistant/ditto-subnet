"""Replay protection for signed validator API requests."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ditto.db.models import ValidatorRequestNonce

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ValidatorRequestReplayError(Exception):
    """Raised when a validator request reuses a consumed nonce."""


VALIDATOR_NONCE_JANITOR_LOCK_KEY = "ditto:validator-nonce-janitor:v1"


async def consume_validator_nonce(
    session: AsyncSession,
    *,
    nonce: UUID,
    validator_hotkey: str,
    now: datetime,
    expires_at: datetime,
) -> None:
    """Atomically consume ``nonce`` or reject it as a replay.

    A nested transaction contains the uniqueness violation so callers can map a
    replay to HTTP 409 without poisoning their surrounding ticket transaction.
    """
    try:
        async with session.begin_nested():
            session.add(
                ValidatorRequestNonce(
                    nonce=nonce,
                    validator_hotkey=validator_hotkey,
                    used_at=now,
                    expires_at=expires_at,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        raise ValidatorRequestReplayError(
            "validator request nonce already used"
        ) from exc


async def delete_expired_validator_nonces(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> int | None:
    """Delete at most ``limit`` expired guards, or return ``None`` if busy.

    The advisory lock is transaction scoped, so replica-local janitors cannot
    multiply the configured batch size. ``SKIP LOCKED`` also keeps the sweep
    independent from a request concurrently inserting or inspecting a guard.
    """
    if limit <= 0:
        raise ValueError("validator nonce janitor limit must be positive")
    if session.get_bind().dialect.name != "postgresql":
        # SQLite-backed unit tests exercise replay consumption, not the
        # production janitor. Keeping this function PostgreSQL-only prevents a
        # test-only query from drifting away from the SQL actually deployed.
        return 0
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": VALIDATOR_NONCE_JANITOR_LOCK_KEY},
    )
    if not locked:
        return None
    result = await session.execute(
        text(
            """
            WITH expired AS (
                SELECT nonce
                FROM validator_request_nonces
                WHERE expires_at < :now
                ORDER BY expires_at, nonce
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM validator_request_nonces AS nonce
            USING expired
            WHERE nonce.nonce = expired.nonce
            RETURNING nonce.nonce
            """
        ),
        {"now": now, "limit": limit},
    )
    return len(result.all())


__all__ = [
    "VALIDATOR_NONCE_JANITOR_LOCK_KEY",
    "ValidatorRequestReplayError",
    "consume_validator_nonce",
    "delete_expired_validator_nonces",
]

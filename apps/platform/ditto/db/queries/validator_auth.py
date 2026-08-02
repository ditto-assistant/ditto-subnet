"""Replay protection for signed validator API requests."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from ditto.db.models import ValidatorRequestNonce

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ValidatorRequestReplayError(Exception):
    """Raised when a validator request reuses a consumed nonce."""


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
    await session.execute(
        delete(ValidatorRequestNonce).where(ValidatorRequestNonce.expires_at < now)
    )
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

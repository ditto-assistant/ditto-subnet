"""Serialize confirmation policy changes with costly bundle issuance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_LOCK_KEY = "v9-confirmation-global-policy"


async def lock_confirmation_policy(session: AsyncSession) -> None:
    """Take the transaction lock shared by settings writes and new claims."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(_LOCK_KEY, 0)))
        )


__all__ = ["lock_confirmation_policy"]

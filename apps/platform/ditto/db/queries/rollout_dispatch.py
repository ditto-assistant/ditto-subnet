"""One fail-fast transaction fence for validator job dispatch.

Every ``POST /validator/job`` lane can touch validator, re-test, ticket, agent,
and rollout rows in a different shape.  A single transaction-scoped advisory
lock taken before any of those row locks gives the whole allocator one lock
order.  It is deliberately non-blocking: idle workers poll again, so queueing
inside Postgres only turns harmless contention into API saturation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


ROLLOUT_DISPATCH_LOCK_KEY = "ditto:validator-rollout-dispatch:v1"


async def try_lock_rollout_dispatch(session: AsyncSession) -> bool:
    """Take the one allocator fence for this transaction, without waiting."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": ROLLOUT_DISPATCH_LOCK_KEY},
        )
    )


__all__ = ["ROLLOUT_DISPATCH_LOCK_KEY", "try_lock_rollout_dispatch"]

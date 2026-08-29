"""Append-only screening-policy activation reads and writes.

Backs ``ditto.api_server.endpoints.admin_screener_policy_activation`` (operator
writes) and ``ditto.api_server.screener_policy_activation`` (the hot-path
required-version read). Append-only by contract: no UPDATE, no DELETE, so the
operator audit trail is complete and immutable.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import ScreenerPolicyActivation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def latest_screener_policy_activation(
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> ScreenerPolicyActivation | None:
    """The newest schedule revision, or ``None`` when none was ever written."""
    stmt = (
        select(ScreenerPolicyActivation)
        .order_by(ScreenerPolicyActivation.revision.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def governing_screener_policy_activation(
    session: AsyncSession, *, now: datetime
) -> ScreenerPolicyActivation | None:
    """The newest revision whose ``activate_at`` has passed.

    A newer not-yet-due revision never pulls the required version down: only
    due activations govern, and among those the newest revision wins.
    """
    return await session.scalar(
        select(ScreenerPolicyActivation)
        .where(ScreenerPolicyActivation.activate_at <= now)
        .order_by(ScreenerPolicyActivation.revision.desc())
        .limit(1)
    )


async def list_screener_policy_activations(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[ScreenerPolicyActivation]:
    """The append-only history, newest first (for the operator console)."""
    return list(
        await session.scalars(
            select(ScreenerPolicyActivation)
            .order_by(ScreenerPolicyActivation.revision.desc())
            .limit(limit)
        )
    )


async def insert_screener_policy_activation(
    session: AsyncSession,
    *,
    parent_revision: int,
    target_policy_version: int,
    activate_at: datetime,
    rescreen_scored: bool,
    reason: str,
    actor: str,
) -> ScreenerPolicyActivation:
    """Append one immutable schedule revision (caller-managed transaction).

    Flushes immediately so a concurrent write racing the same
    ``parent_revision`` surfaces as ``IntegrityError`` here (the caller maps it
    to a 409) rather than at commit.
    """
    row = ScreenerPolicyActivation(
        parent_revision=parent_revision,
        target_policy_version=target_policy_version,
        activate_at=activate_at,
        rescreen_scored=rescreen_scored,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row

"""Append-only emission-burn settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import BurnSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_burn_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> BurnSettingsRevision | None:
    return await session.scalar(
        select(BurnSettingsRevision)
        .where(BurnSettingsRevision.scope == scope)
        .order_by(BurnSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_burn_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[BurnSettingsRevision]:
    return list(
        await session.scalars(
            select(BurnSettingsRevision)
            .order_by(BurnSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_burn_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> BurnSettingsRevision:
    row = BurnSettingsRevision(
        parent_revision=parent_revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row

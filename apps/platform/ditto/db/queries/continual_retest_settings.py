"""Append-only continual-retest settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import ContinualRetestSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_continual_retest_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> ContinualRetestSettingsRevision | None:
    return await session.scalar(
        select(ContinualRetestSettingsRevision)
        .where(ContinualRetestSettingsRevision.scope == scope)
        .order_by(ContinualRetestSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_continual_retest_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[ContinualRetestSettingsRevision]:
    return list(
        await session.scalars(
            select(ContinualRetestSettingsRevision)
            .order_by(ContinualRetestSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_continual_retest_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> ContinualRetestSettingsRevision:
    row = ContinualRetestSettingsRevision(
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

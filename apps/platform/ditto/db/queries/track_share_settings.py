"""Append-only track-share settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import TrackShareSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_track_share_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> TrackShareSettingsRevision | None:
    return await session.scalar(
        select(TrackShareSettingsRevision)
        .where(TrackShareSettingsRevision.scope == scope)
        .order_by(TrackShareSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_track_share_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[TrackShareSettingsRevision]:
    return list(
        await session.scalars(
            select(TrackShareSettingsRevision)
            .order_by(TrackShareSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_track_share_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> TrackShareSettingsRevision:
    row = TrackShareSettingsRevision(
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

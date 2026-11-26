"""Append-only Ditto Edge lane settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import EdgeLaneSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_edge_lane_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> EdgeLaneSettingsRevision | None:
    return await session.scalar(
        select(EdgeLaneSettingsRevision)
        .where(EdgeLaneSettingsRevision.scope == scope)
        .order_by(EdgeLaneSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_edge_lane_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[EdgeLaneSettingsRevision]:
    return list(
        await session.scalars(
            select(EdgeLaneSettingsRevision)
            .order_by(EdgeLaneSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_edge_lane_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> EdgeLaneSettingsRevision:
    row = EdgeLaneSettingsRevision(
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

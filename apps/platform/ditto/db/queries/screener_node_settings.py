"""Resolve append-only per-node screener concurrency settings."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_node_settings import ScreenerNodeChannelSettings
from ditto.db.models import ScreenerNodeChannelSettingsRevision

DEFAULT_SCREENER_NODE_CHANNEL_SETTINGS = ScreenerNodeChannelSettings()


async def latest_screener_node_channel_settings(
    session: AsyncSession, *, node_id: str
) -> ScreenerNodeChannelSettingsRevision | None:
    return await session.scalar(
        select(ScreenerNodeChannelSettingsRevision)
        .where(ScreenerNodeChannelSettingsRevision.node_id == node_id)
        .order_by(desc(ScreenerNodeChannelSettingsRevision.revision))
        .limit(1)
    )


async def resolve_screener_node_channel_settings(
    session: AsyncSession, *, node_id: str
) -> tuple[int, ScreenerNodeChannelSettings]:
    row = await latest_screener_node_channel_settings(session, node_id=node_id)
    if row is None:
        return 0, DEFAULT_SCREENER_NODE_CHANNEL_SETTINGS
    return row.revision, ScreenerNodeChannelSettings.model_validate(row.settings)

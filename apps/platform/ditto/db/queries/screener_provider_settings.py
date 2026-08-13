"""Resolve the current screener provider routing revision."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_provider_settings import ScreenerProviderSettings
from ditto.db.models import ScreenerProviderSettingsRevision

DEFAULT_SCREENER_PROVIDER_SETTINGS = ScreenerProviderSettings()


async def latest_screener_provider_settings(
    session: AsyncSession, *, environment: str
) -> ScreenerProviderSettingsRevision | None:
    return await session.scalar(
        select(ScreenerProviderSettingsRevision)
        .where(ScreenerProviderSettingsRevision.environment == environment)
        .order_by(desc(ScreenerProviderSettingsRevision.revision))
        .limit(1)
    )


async def resolve_screener_provider_settings(
    session: AsyncSession, *, environment: str
) -> tuple[int, ScreenerProviderSettings]:
    row = await latest_screener_provider_settings(session, environment=environment)
    if row is None:
        return 0, DEFAULT_SCREENER_PROVIDER_SETTINGS
    return row.revision, ScreenerProviderSettings.model_validate(row.settings)

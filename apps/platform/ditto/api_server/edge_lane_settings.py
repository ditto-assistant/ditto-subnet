"""Short-TTL resolver for the Ditto Edge shadow lane settings."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.edge_lane import EdgeLaneSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import EdgeLaneSettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = EdgeLaneSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class ResolvedEdgeLaneSettings:
    settings: EdgeLaneSettings
    revision: int


def settings_from_row(row: EdgeLaneSettingsRevision | None) -> EdgeLaneSettings:
    """The stored policy, or the default (lane off).

    An unreadable revision falls back to the default rather than raising. The
    default is the safe direction: the lane is off, so a bad row stops the
    shadow run and pays or serves nothing.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return EdgeLaneSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "edge lane settings revision %s is invalid; the lane stays off",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass
class _CacheEntry:
    resolved: ResolvedEdgeLaneSettings
    loaded_at: float


class EdgeLaneSettingsResolver:
    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> ResolvedEdgeLaneSettings:
        if session_maker is None:
            return ResolvedEdgeLaneSettings(DEFAULT_SETTINGS, 0)
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.resolved
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.resolved
            from ditto.db.queries.edge_lane_settings import (
                latest_edge_lane_settings_revision,
            )

            async with session_maker() as session:
                row = await latest_edge_lane_settings_revision(session)
            resolved = ResolvedEdgeLaneSettings(
                settings_from_row(row), row.revision if row is not None else 0
            )
            self._cache = _CacheEntry(resolved=resolved, loaded_at=time.monotonic())
            return resolved

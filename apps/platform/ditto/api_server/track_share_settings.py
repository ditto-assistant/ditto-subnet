"""Short-TTL resolver for the operator-governed per-track emission split."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.track_share_settings import TrackShareSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import TrackShareSettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = TrackShareSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def settings_from_row(row: TrackShareSettingsRevision | None) -> TrackShareSettings:
    """The stored policy, or the unset default.

    An unparseable revision falls back to the default (nothing served, so
    validators fold their compiled memory-only split) instead of failing the
    ledger read every validator makes before every weight submission.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return TrackShareSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "track share settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass
class _CacheEntry:
    settings: TrackShareSettings
    loaded_at: float


class TrackShareSettingsResolver:
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
    ) -> TrackShareSettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            from ditto.db.queries.track_share_settings import (
                latest_track_share_settings_revision,
            )

            async with session_maker() as session:
                row = await latest_track_share_settings_revision(session)
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings

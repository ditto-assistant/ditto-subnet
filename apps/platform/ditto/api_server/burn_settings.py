"""Short-TTL resolver for the operator-controlled emission burn share."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.burn_settings import BurnSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import BurnSettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = BurnSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def settings_from_row(row: BurnSettingsRevision | None) -> BurnSettings:
    """The stored policy, or the no-burn default.

    An unparseable revision falls back to the default rather than propagating
    the error: this value is read on the ledger path that every validator hits
    before every weight submission, and failing that read to protect a malformed
    row would stop weight submission subnet-wide. The default is also the
    conservative direction -- it pays miners, it does not withhold from them.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return BurnSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "burn settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass
class _CacheEntry:
    settings: BurnSettings
    loaded_at: float


class BurnSettingsResolver:
    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        self._cache = None

    async def resolve(self, session_maker: async_sessionmaker | None) -> BurnSettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            from ditto.db.queries.burn_settings import latest_burn_settings_revision

            async with session_maker() as session:
                row = await latest_burn_settings_revision(session)
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings

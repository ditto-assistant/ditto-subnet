"""Resolver for the operator-controlled scoring lease clocks (#1156).

Short-TTL cached like ``ditto.api_server.inference_concurrency_settings``: every
validator poll issues or resumes a lease, and the TTL changes a few times a
month. The resolver reads on its **own** session, so call it before the caller
opens its dispatch transaction.

Fails **open onto the shipped default** (the pre-#1156 180-minute constant) on a
missing, corrupt, or unreadable row. The default is the configuration the fleet
already ran, so the worst case of a bad revision is the old lease length.

The resolved TTL is stamped onto a ticket only when it is minted. Nothing here
touches a live ticket's deadline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ditto.api_models.scoring_lease_settings import ScoringLeaseSettings
from ditto.db.queries.scoring_lease_settings import (
    latest_scoring_lease_settings_revision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import ScoringLeaseSettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = ScoringLeaseSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def settings_from_row(
    row: ScoringLeaseSettingsRevision | None,
) -> ScoringLeaseSettings:
    """Decode a revision, falling back to the default on a corrupt payload."""
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return ScoringLeaseSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "scoring lease settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass
class _CacheEntry:
    settings: ScoringLeaseSettings
    loaded_at: float


class ScoringLeaseSettingsResolver:
    """Short-TTL cache for the ticket-issue read."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next lease sees a just-written revision."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> ScoringLeaseSettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            try:
                async with session_maker() as session:
                    row = await latest_scoring_lease_settings_revision(session)
            except Exception:
                # A database blip must not fail a job poll. Serve the default
                # and do NOT cache it, so the next poll re-reads.
                logger.warning(
                    "could not read scoring lease settings; using defaults",
                    exc_info=True,
                )
                return DEFAULT_SETTINGS
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings


async def resolve_scoring_lease_settings(app_state: Any) -> ScoringLeaseSettings:
    """The policy ticket issuance applies, from a request's ``app.state``.

    An app built without the resolver (unit tests, or an entry point that forgot
    to wire it) gets the shipped default, which is the pre-#1156 behaviour.
    """
    resolver: ScoringLeaseSettingsResolver | None = getattr(
        app_state, "scoring_lease_settings", None
    )
    if resolver is None:
        return DEFAULT_SETTINGS
    return await resolver.resolve(getattr(app_state, "session_maker", None))


async def resolve_scoring_ticket_ttl(app_state: Any) -> timedelta:
    """The TTL to stamp on the next canonical or replacement scoring ticket."""
    return (await resolve_scoring_lease_settings(app_state)).scoring_ticket_ttl

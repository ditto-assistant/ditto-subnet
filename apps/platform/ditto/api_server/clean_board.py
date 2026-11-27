"""The clean-board unpaid fraction: one pure rule, one resolver (#2911).

:func:`unpaid_fraction` is the single rule. The validator ledger, the epoch
pin and the public board all reach the same verdict from the same facts, so
the board can publish exactly what the fleet folds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.clean_board import CleanBoardSettings

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import CleanBoardSettingsRevision
    from ditto.db.queries.clean_board import CleanBoardFacts

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = CleanBoardSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def unpaid_fraction(
    facts: CleanBoardFacts | None,
    settings: CleanBoardSettings,
    *,
    now: datetime,
) -> float | None:
    """The burned fraction of one agent's slot, or ``None`` when it is paid in full.

    Paid in full while board-attested, before it has finalized, and inside its
    grace window. A zero share burns nothing, so it is reported as no fraction.
    Independent of ``settings.enabled``: shadow computes the same numbers.
    """
    if facts is None or facts.attested or facts.finalized_at is None:
        return None
    if now < facts.finalized_at + timedelta(seconds=settings.attestation_grace_seconds):
        return None
    share = settings.unattested_slot_burn_share
    return share if share > 0.0 else None


def unpaid_fractions(
    agent_ids: Iterable[UUID],
    facts: Mapping[UUID, CleanBoardFacts],
    settings: CleanBoardSettings,
    *,
    now: datetime,
) -> dict[UUID, float]:
    """``agent_id -> fraction`` for every listed agent that is not paid in full."""
    fractions: dict[UUID, float] = {}
    for agent_id in agent_ids:
        fraction = unpaid_fraction(facts.get(agent_id), settings, now=now)
        if fraction is not None:
            fractions[agent_id] = fraction
    return fractions


def settings_from_row(row: CleanBoardSettingsRevision | None) -> CleanBoardSettings:
    """The stored policy, or the disabled default.

    An unparseable revision falls back to the default instead of failing the
    ledger read every validator makes before every weight submission. The
    default serves nothing, so the fallback direction pays miners.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return CleanBoardSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "clean board settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass
class _CacheEntry:
    settings: CleanBoardSettings
    loaded_at: float


class CleanBoardSettingsResolver:
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
    ) -> CleanBoardSettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            from ditto.db.queries.clean_board import (
                latest_clean_board_settings_revision,
            )

            async with session_maker() as session:
                row = await latest_clean_board_settings_revision(session)
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings

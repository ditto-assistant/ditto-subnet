"""Resolvers for the operator-controlled validator-queue policy.

Two read profiles, deliberately kept apart:

:func:`resolve_queue_policy_settings` is **uncached**. It serves the
    freeze-at-rollout-start read (``POST /admin/benchmark-rollout/{v}``), which
    happens once per rollout. A cache there could only add a window in which an
    operator's fresh revision is silently ignored by the very next start, which
    is the one moment it must not be.

:class:`QueuePolicySettingsResolver` is **short-TTL cached**, matching
    ``ditto.api_server.continual_retest_settings``. It serves the lane split,
    the contender lane and the carryover gate, which are consulted on every
    validator job poll, so a per-poll settings SELECT would be real load on the
    hot path for a value that changes a few times a week.

Both fail **open onto the shipped defaults** on a corrupt or unreadable row: the
defaults are byte-identical to the behaviour before this board existed, so the
worst case of a bad revision is the old hard-coded queue, never a stalled one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.queue_policy_settings import QueuePolicySettings
from ditto.db.queries.queue_policy_settings import (
    latest_queue_policy_settings_revision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.db.models import QueuePolicySettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = QueuePolicySettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


# Keys that were removed from the policy and must be ignored when a revision
# written before the removal is read back.
#
# ``prev_gen_carryover`` is stored WHOLE, and the model is ``extra="forbid"``.
# Without this, a revision that still carries a retired key fails validation --
# and because this decoder fails open, the operator's ENTIRE policy (cohort
# sizes, lane cycle, owner limits, all of it) would silently revert to the
# shipped defaults behind one log line. Dropping the dead key instead keeps
# every setting the operator actually chose.
#
# Read-side only, and deliberately so: the WRITE path stays strict, so an
# operator or a stale Backroom client that still sends the key is REJECTED
# rather than quietly ignored. Forgiving a stored document is not the same as
# accepting a new instruction.
_RETIRED_CARRYOVER_KEYS = frozenset({"allow_retired_era_backfill"})


def _without_retired_keys(settings: object) -> object:
    """Strip removed keys from a stored payload, leaving everything else alone."""
    if not isinstance(settings, dict):
        return settings
    carryover = settings.get("prev_gen_carryover")
    if not isinstance(carryover, dict):
        return settings
    retired = _RETIRED_CARRYOVER_KEYS & carryover.keys()
    if not retired:
        return settings
    logger.info(
        "dropping retired queue-policy keys %s from a stored revision",
        sorted(retired),
    )
    return {
        **settings,
        "prev_gen_carryover": {
            key: value for key, value in carryover.items() if key not in retired
        },
    }


def settings_from_row(
    row: QueuePolicySettingsRevision | None,
) -> QueuePolicySettings:
    """Decode a revision, falling back to the defaults on a corrupt payload.

    Fails **open** onto the shipped defaults rather than raising: an unreadable
    row must not be able to wedge the validator queue or block an operator from
    starting a rollout, and the defaults are the behaviour every prior release
    had.

    Keys retired since the revision was written are dropped first, so removing
    a field does not turn every older revision into a silent policy reset.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return QueuePolicySettings.model_validate(_without_retired_keys(row.settings))
    except ValidationError:
        logger.warning(
            "queue policy settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


async def resolve_queue_policy_settings(
    session: AsyncSession,
) -> QueuePolicySettings:
    """The uncached policy, for the freeze-at-rollout-start read."""
    return settings_from_row(await latest_queue_policy_settings_revision(session))


@dataclass
class _CacheEntry:
    settings: QueuePolicySettings
    loaded_at: float


class QueuePolicySettingsResolver:
    """Short-TTL cache for the per-poll queue-policy reads."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next read sees a just-written revision."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> QueuePolicySettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            async with session_maker() as session:
                row = await latest_queue_policy_settings_revision(session)
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings

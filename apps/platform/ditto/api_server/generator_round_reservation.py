"""Whether validator slots are reserved for generator-competition round work.

The validator slot policy (``ValidatorSlotSettings.generator_round_reserved_slots``
and its per-validator overrides) holds slots back for round panel runs, but only
while rounds can actually be leased: the competition is not ``off`` and
``validator_rounds_enabled`` is set. With rounds disabled nothing is reserved, so
a reservation can never idle capacity for work that cannot arrive. For the
same reason a validator whose heartbeat does not advertise round support
(:func:`validator_advertises_rounds`) reserves nothing either.

The canonical issue path consults this on every poll, so the read is cached for
a short TTL on ``app.state.generator_round_reservation``, mirroring the slot
settings resolver. The admin generator-settings write invalidates its worker's
cache; other workers converge within the TTL.

**Failure answers "not active".** A failed or corrupt read is exactly the case
in which :func:`~ditto.api_server.generator_competition.load_settings` closes
the round lane itself, so reserving slots for it then would only idle capacity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ditto.api_models.validator import ValidatorCapabilities
from ditto.api_server.generator_competition import load_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_models.generator import GeneratorCompetitionSettings

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 5.0
"""Same bound as the slot settings resolver: a toggle lands within seconds."""


def rounds_reserve_slots(settings: GeneratorCompetitionSettings) -> bool:
    """Whether this generator policy leases round work to validators."""
    return settings.mode != "off" and settings.validator_rounds_enabled


def validator_advertises_rounds(heartbeat: Any) -> bool:
    """Whether this validator's stored heartbeat advertises round support.

    A reservation on a validator that can never be issued round work would
    only idle a slot, so the reservation applies to round-capable validators
    alone. This reads the signed capability, deliberately without the freshness
    window the round lane itself checks: a scorer report that lapses for a few
    seconds must not briefly release the reserved slot to a 90-minute canonical
    lease and starve the round it was held for. A validator that is actually
    offline receives no work on any lane anyway.
    """
    if heartbeat is None or getattr(heartbeat, "protocol_version", 0) < 7:
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate(
            getattr(heartbeat, "capabilities", None)
        )
    except ValidationError:
        return False
    scorer = capabilities.scorer_benchmarks
    return scorer is not None and scorer.generator_rounds


@dataclass
class _CacheEntry:
    active: bool
    loaded_at: float


class GeneratorRoundReservationResolver:
    """TTL-cached answer to "are round slots reserved right now?"."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        self._cache = None

    def _fresh(self) -> bool | None:
        cache = self._cache
        if cache is not None and (time.monotonic() - cache.loaded_at) < self._ttl:
            return cache.active
        return None

    async def resolve(self, session_maker: async_sessionmaker) -> bool:
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh()
            if cached is not None:
                return cached
            try:
                async with session_maker() as session:
                    resolved = await load_settings(session)
            except Exception:
                # Broad on purpose, like the slot settings resolver: any read
                # failure answers "not reserved" without caching, and never 500s
                # the dispatch path.
                logger.warning(
                    "generator round settings read failed; reserving no round slots",
                    exc_info=True,
                )
                return False
            active = rounds_reserve_slots(resolved.settings)
            self._cache = _CacheEntry(active=active, loaded_at=time.monotonic())
            return active


async def round_reservation_active(app_state: Any, session: AsyncSession) -> bool:
    """Resolve the reservation gate from a request's ``app.state``.

    Uses the cached resolver on its own session when the app is fully wired.
    An app built without lifespan (no session maker, as in endpoint tests) reads
    the governing revision on ``session`` instead -- a plain indexed SELECT,
    safe inside the caller's open transaction.
    """
    resolver: GeneratorRoundReservationResolver | None = getattr(
        app_state, "generator_round_reservation", None
    )
    session_maker = getattr(app_state, "session_maker", None)
    if resolver is not None and session_maker is not None:
        return await resolver.resolve(session_maker)
    return rounds_reserve_slots((await load_settings(session)).settings)


def invalidate_round_reservation(app_state: Any) -> None:
    resolver: GeneratorRoundReservationResolver | None = getattr(
        app_state, "generator_round_reservation", None
    )
    if resolver is not None:
        resolver.invalidate()

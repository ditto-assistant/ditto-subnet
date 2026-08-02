"""Compute-time resolver for the hot-swappable efficiency-bonus policy.

The three efficiency read points — ``ensure_efficiency_state`` and
``read_efficiency_board`` (``efficiency.py``) and the validator fold in
``scoring.py`` — no longer read the boot-time
:class:`~ditto.api_server.config.EfficiencyBonusConfig` directly. They resolve
the *effective* config through :class:`EfficiencyBonusSettingsResolver`, which
overlays the latest append-only revision (if any) onto the env seed and caches
it for a short TTL. A backroom write therefore lands on the next compute /
leaderboard read with no redeploy; with no revision written the seed governs,
so behavior is byte-identical to before #403's follow-up.

**Why an independent session:** the leaderboard calls ``ensure_efficiency_state``
first, and that opens its own ``session.begin()`` — which raises if the request
session already autobegan a transaction. So the resolver never touches the
request session; it reads the (tiny, indexed) latest-revision row on a
short-lived session from the app's session maker. When no maker is wired
(unit tests without lifespan) it simply returns the seed.

**fold requires enabled** is enforced *here*, at read time (:func:`effective_config`
clamps ``fold_enabled`` to ``False`` whenever ``enabled`` is ``False``), so the
invariant holds even for a hand-edited row — not only at boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.efficiency_settings import (
    EffectiveEfficiencyBonusSettings,
    EfficiencyBonusSettings,
)
from ditto.api_server.config import EfficiencyBonusConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import EfficiencyBonusSettingsRevision

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_TTL_SECONDS = 5.0
"""Upper bound on how long a backroom change can take to reach the compute path
(one worker also invalidates its own cache immediately on write). Small so a
canary flip is observable within seconds; nonzero so a hot leaderboard does not
issue a settings read on literally every request."""


def seed_settings(seed: EfficiencyBonusConfig) -> EfficiencyBonusSettings:
    """The env seed rendered as a settings object (the default when no
    revision exists). The seed already satisfied ``check_config`` at boot."""
    return EfficiencyBonusSettings(
        enabled=seed.enabled,
        fold_enabled=seed.fold_enabled,
        cap=seed.cap,
        deep_cap=seed.deep_cap,
        deep_frontier_ratio=seed.deep_frontier_ratio,
        cohort_size=seed.cohort_size,
        min_cohort=seed.min_cohort,
        epoch_hours=seed.epoch_hours,
        quality_floor=seed.quality_floor,
        memory_floor=seed.memory_floor,
    )


def effective_config(
    seed: EfficiencyBonusConfig, settings: EfficiencyBonusSettings | None
) -> EfficiencyBonusConfig:
    """Overlay a revision's settings onto the env seed, enforcing at read time
    the invariant that folding requires the bonus to be enabled.

    ``settings=None`` (no revision) returns the seed unchanged, so an untouched
    deployment is byte-identical to pre-change.
    """
    if settings is None:
        # The seed passed check_config at boot (incl. fold => enabled); clamp
        # anyway so this function has a single, total invariant.
        return replace(seed, fold_enabled=seed.fold_enabled and seed.enabled)
    return EfficiencyBonusConfig(
        enabled=settings.enabled,
        fold_enabled=settings.fold_enabled and settings.enabled,
        cap=settings.cap,
        deep_cap=settings.deep_cap,
        deep_frontier_ratio=settings.deep_frontier_ratio,
        cohort_size=settings.cohort_size,
        min_cohort=settings.min_cohort,
        epoch_hours=settings.epoch_hours,
        quality_floor=settings.quality_floor,
        memory_floor=settings.memory_floor,
    )


def settings_from_row(
    row: EfficiencyBonusSettingsRevision | None,
) -> EfficiencyBonusSettings | None:
    """Parse a persisted revision's JSON into settings, or ``None`` if the row
    is absent or no longer parses (schema drift / hand edit → fall back to seed
    rather than crash the compute path)."""
    if row is None:
        return None
    try:
        return EfficiencyBonusSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "efficiency bonus settings revision %s no longer parses; "
            "falling back to the env seed",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return None


@dataclass
class _CacheEntry:
    config: EfficiencyBonusConfig
    loaded_at: float


class EfficiencyBonusSettingsResolver:
    """TTL-cached read of the effective efficiency-bonus config.

    One instance lives on ``app.state.efficiency_settings``. Safe for concurrent
    use: a single in-flight reload is serialized so a burst of reads issues at
    most one settings query per TTL window.
    """

    def __init__(
        self,
        seed: EfficiencyBonusConfig,
        *,
        ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS,
    ) -> None:
        self._seed = seed
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def seed(self) -> EfficiencyBonusConfig:
        return self._seed

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next read re-reads the DB. Called by the admin
        endpoint after a write so a change on this worker lands immediately
        (other workers/processes converge within the TTL)."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> EfficiencyBonusConfig:
        """The effective config for the current compute/leaderboard read.

        Returns the seed unchanged when no session maker is available (unit
        tests without lifespan) or when no revision has been written.
        """
        if session_maker is None:
            return effective_config(self._seed, None)
        now = time.monotonic()
        cache = self._cache
        if cache is not None and (now - cache.loaded_at) < self._ttl:
            return cache.config
        async with self._lock:
            cache = self._cache
            now = time.monotonic()
            if cache is not None and (now - cache.loaded_at) < self._ttl:
                return cache.config
            settings = await self._load_latest(session_maker)
            config = effective_config(self._seed, settings)
            self._cache = _CacheEntry(config=config, loaded_at=time.monotonic())
            return config

    async def _load_latest(
        self, session_maker: async_sessionmaker
    ) -> EfficiencyBonusSettings | None:
        from ditto.db.queries.efficiency_settings import (
            latest_efficiency_settings_revision,
        )

        async with session_maker() as session:
            row = await latest_efficiency_settings_revision(session)
        return settings_from_row(row)


def effective_view(
    seed: EfficiencyBonusConfig,
    row: EfficiencyBonusSettingsRevision | None,
    *,
    ttl_seconds: float,
) -> EffectiveEfficiencyBonusSettings:
    """The operator-console view of what the compute path resolves to, built
    from the freshly-read latest revision (never the TTL cache) so the console
    always reflects current DB truth."""
    settings = settings_from_row(row)
    config = effective_config(seed, settings)
    if row is not None and settings is not None:
        revision = row.revision
        scope = row.scope
        checksum = row.checksum
        source = "revision"
    else:
        revision = 0
        scope = "*"
        checksum = ""
        source = "seed"
        settings = seed_settings(seed)
    return EffectiveEfficiencyBonusSettings(
        revision=revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        source=source,
        fold_effective=config.fold_enabled,
        max_age_seconds=ttl_seconds,
    )

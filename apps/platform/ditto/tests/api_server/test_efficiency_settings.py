"""Unit tests for the hot-swappable efficiency-bonus settings resolver.

Covers the read-time overlay of a revision onto the env seed, the
``fold requires enabled`` clamp enforced at read time, corrupt-row fallback,
and the TTL cache / invalidate behavior that makes a backroom flip land on the
next compute read with no restart.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.efficiency_settings import EfficiencyBonusSettings
from ditto.api_server.config import EfficiencyBonusConfig
from ditto.api_server.efficiency_settings import (
    EfficiencyBonusSettingsResolver,
    effective_config,
    effective_view,
    seed_settings,
    settings_from_row,
)
from ditto.db.models import EfficiencyBonusSettingsRevision
from ditto.db.queries.efficiency_settings import (
    insert_efficiency_settings_revision,
)


async def _write_revision(
    maker: async_sessionmaker[AsyncSession],
    settings: EfficiencyBonusSettings,
    *,
    parent_revision: int,
) -> None:
    async with maker() as s, s.begin():
        await insert_efficiency_settings_revision(
            s,
            parent_revision=parent_revision,
            scope="*",
            settings=settings.model_dump(mode="json"),
            checksum="a" * 64,
            reason="operator test",
            actor="tester",
        )


class TestEffectiveConfig:
    def test_no_revision_returns_seed(self) -> None:
        seed = EfficiencyBonusConfig(enabled=True, cap=0.07, min_cohort=3)
        assert effective_config(seed, None) == seed

    def test_revision_overlays_all_knobs(self) -> None:
        seed = EfficiencyBonusConfig()
        settings = EfficiencyBonusSettings(
            enabled=True,
            fold_enabled=True,
            cap=0.04,
            deep_cap=0.09,
            deep_frontier_ratio=0.4,
            cohort_size=30,
            min_cohort=5,
            epoch_hours=12,
            quality_floor=0.3,
            memory_floor=0.2,
        )
        cfg = effective_config(seed, settings)
        assert cfg == EfficiencyBonusConfig(
            enabled=True,
            fold_enabled=True,
            cap=0.04,
            deep_cap=0.09,
            deep_frontier_ratio=0.4,
            cohort_size=30,
            min_cohort=5,
            epoch_hours=12,
            quality_floor=0.3,
            memory_floor=0.2,
        )

    def test_fold_requires_enabled_at_read_time(self) -> None:
        # A persisted row can carry fold_enabled=True with enabled=False; the
        # invariant is enforced here, at read time, not only at boot.
        settings = EfficiencyBonusSettings(enabled=False, fold_enabled=True)
        cfg = effective_config(EfficiencyBonusConfig(), settings)
        assert cfg.enabled is False
        assert cfg.fold_enabled is False

    def test_seed_fold_clamped_when_seed_disabled(self) -> None:
        # Defensive: even the seed path clamps (the boot check already forbids
        # this combination, so this is belt-and-suspenders).
        seed = EfficiencyBonusConfig(enabled=False, fold_enabled=True)
        assert effective_config(seed, None).fold_enabled is False


class TestSeedRoundTrip:
    def test_seed_settings_round_trips_through_effective_config(self) -> None:
        seed = EfficiencyBonusConfig(
            enabled=True, fold_enabled=True, cap=0.06, min_cohort=4, epoch_hours=6
        )
        assert effective_config(seed, seed_settings(seed)) == seed


class TestSettingsFromRow:
    def test_none_row_is_none(self) -> None:
        assert settings_from_row(None) is None

    def test_valid_row_parses(self) -> None:
        row = EfficiencyBonusSettingsRevision(
            revision=1,
            parent_revision=0,
            scope="*",
            settings={"enabled": True, "cap": 0.05},
            checksum="a" * 64,
            reason="r",
            actor="a",
        )
        parsed = settings_from_row(row)
        assert parsed is not None and parsed.enabled is True

    def test_corrupt_row_falls_back_to_none(self) -> None:
        # cap out of bounds: a hand-edited / schema-drifted row must not crash
        # the compute path — it falls back to the seed.
        row = EfficiencyBonusSettingsRevision(
            revision=1,
            parent_revision=0,
            scope="*",
            settings={"enabled": True, "cap": 5.0},
            checksum="a" * 64,
            reason="r",
            actor="a",
        )
        assert settings_from_row(row) is None


class TestResolver:
    async def test_no_session_maker_returns_seed(self) -> None:
        seed = EfficiencyBonusConfig(enabled=True, cap=0.05, min_cohort=3)
        resolver = EfficiencyBonusSettingsResolver(seed, ttl_seconds=0)
        assert await resolver.resolve(None) == seed

    async def test_reads_latest_revision(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        resolver = EfficiencyBonusSettingsResolver(
            EfficiencyBonusConfig(), ttl_seconds=0
        )
        assert (await resolver.resolve(session_maker)).enabled is False
        await _write_revision(
            session_maker,
            EfficiencyBonusSettings(enabled=True, min_cohort=3),
            parent_revision=0,
        )
        # ttl=0 → the next read reflects the new revision with no restart.
        cfg = await resolver.resolve(session_maker)
        assert cfg.enabled is True
        assert cfg.min_cohort == 3

    async def test_ttl_cache_serves_stale_until_invalidated(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        resolver = EfficiencyBonusSettingsResolver(
            EfficiencyBonusConfig(), ttl_seconds=3600
        )
        assert (await resolver.resolve(session_maker)).enabled is False
        await _write_revision(
            session_maker,
            EfficiencyBonusSettings(enabled=True, min_cohort=3),
            parent_revision=0,
        )
        # Within the TTL the cached (disabled) value is still served.
        assert (await resolver.resolve(session_maker)).enabled is False
        # invalidate() (what the admin endpoint calls after a write) makes the
        # change land immediately.
        resolver.invalidate()
        assert (await resolver.resolve(session_maker)).enabled is True

    async def test_latest_revision_wins(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        resolver = EfficiencyBonusSettingsResolver(
            EfficiencyBonusConfig(), ttl_seconds=0
        )
        await _write_revision(
            session_maker,
            EfficiencyBonusSettings(enabled=True, cap=0.05, min_cohort=3),
            parent_revision=0,
        )
        await _write_revision(
            session_maker,
            EfficiencyBonusSettings(enabled=True, cap=0.08, min_cohort=3),
            parent_revision=1,
        )
        assert (await resolver.resolve(session_maker)).cap == 0.08


class TestEffectiveView:
    def test_seed_view_when_no_revision(self) -> None:
        seed = EfficiencyBonusConfig(enabled=False)
        view = effective_view(seed, None, ttl_seconds=5.0)
        assert view.source == "seed"
        assert view.revision == 0
        assert view.fold_effective is False
        assert view.max_age_seconds == 5.0

    def test_revision_view_reports_fold_clamp(self) -> None:
        seed = EfficiencyBonusConfig()
        row = EfficiencyBonusSettingsRevision(
            revision=4,
            parent_revision=3,
            scope="*",
            settings={"enabled": False, "fold_enabled": True},
            checksum="b" * 64,
            reason="r",
            actor="a",
        )
        view = effective_view(seed, row, ttl_seconds=0.0)
        assert view.source == "revision"
        assert view.revision == 4
        # fold requested but enabled off → not in force.
        assert view.settings.fold_enabled is True
        assert view.fold_effective is False

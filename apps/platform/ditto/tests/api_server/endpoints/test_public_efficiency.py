"""End-to-end tests for the relative token-efficiency bonus (bench_version 7).

Covers the platform-layer contract from ``docs/relative-efficiency-bonus.md``:
the leaderboard materializes an epoch-frozen cohort snapshot, assigns
insert-once bonuses against its robust reference, exposes base composite /
bonus / effective composite distinctly, honors the N_min activation gate, and
leaves every bench_version < 7 board byte-identical (no snapshot, no bonus
row, null fields). The validator-facing ledger exposes effective_composite
only behind the default-off fold flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server import EfficiencyBonusConfig, create_api_server
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.efficiency import ensure_efficiency_state
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    EfficiencyBonus,
    EfficiencyCohortSnapshot,
)
from ditto.db.queries.scores import upsert_score
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.legacy_era import retired_era_writes_allowed

_VALIDATORS = [
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
]
_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR_HOTKEY = _KEYPAIR.ss58_address
_T0 = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)

_MINERS = [
    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
    "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp",
    "5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw",
]


def _make_app(
    maker: async_sessionmaker[AsyncSession],
    *,
    efficiency: EfficiencyBonusConfig,
) -> FastAPI:
    import os

    # These tests assert mutate-then-refetch sequences; the public TTL cache
    # would serve the stale first body (it has its own middleware coverage).
    os.environ["PUBLIC_CACHE_DISABLED"] = "1"
    app = create_api_server(make_api_server_config(efficiency_bonus=efficiency))
    app.state.commit_hash = "test-commit"

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


_ENABLED = EfficiencyBonusConfig(
    enabled=True,
    cap=0.05,
    cohort_size=25,
    min_cohort=3,
    epoch_hours=24,
)
# Same tuning, switch off. This is the shape an operator actually has when they
# want to LOOK at the boost: the knobs are set, the feature is not turned on.
_DISABLED = replace(_ENABLED, enabled=False)


async def _activate_bench_version(
    maker: async_sessionmaker[AsyncSession], version: int
) -> None:
    async with maker() as s, s.begin():
        s.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=2,
                desired_version=version,
                status="activated",
                cohort_size=5,
                activated_at=_T0,
            )
        )


def _details(total_tokens: int, *, bench_version: int = 7) -> dict:
    return {
        "bench_version": bench_version,
        "token_usage": {
            "status": "complete",
            "total_tokens": total_tokens,
            "usage_unavailable": 0,
        },
        "token_efficiency": {"formula_version": "v7-quality-only-v1"},
    }


async def _seed_finalized(
    maker: async_sessionmaker[AsyncSession],
    *,
    miner: str,
    composite: float,
    total_tokens: int,
    bench_version: int = 7,
    memory_mean: float | None = None,
    sha256: str | None = None,
    normalized_source_hash: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    """One agent with a full k=3 quorum of identical v7 scores + audited usage."""
    agent_id = uuid4()
    async with maker() as s, s.begin():
        s.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=miner,
                name="agent",
                sha256=sha256 or agent_id.hex * 2,
                normalized_source_hash=normalized_source_hash,
                size_bytes=524288,
                status=AgentStatus.SCORED,
                dataset_run_size="full",
                created_at=created_at or _T0,
            )
        )
        await s.flush()
        for i, validator in enumerate(_VALIDATORS):
            await upsert_score(
                s,
                agent_id=agent_id,
                validator_hotkey=validator,
                bench_version=bench_version,
                run_id=f"run_{agent_id.hex[:8]}_{i}",
                seed=42,
                composite=composite,
                tool_mean=composite,
                memory_mean=memory_mean if memory_mean is not None else composite,
                median_ms=500,
                n=110,
                generated_at=_T0 + timedelta(minutes=i),
                signature="ab" * 64,
                details=_details(total_tokens, bench_version=bench_version),
            )
    return agent_id


async def _seed_v7_board(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, UUID]:
    """Three distinct-lineage finalized v7 agents: lean, median, heavy."""
    await _activate_bench_version(maker, 7)
    lean = await _seed_finalized(
        maker, miner=_MINERS[0], composite=0.80, total_tokens=100_000
    )
    mid = await _seed_finalized(
        maker, miner=_MINERS[1], composite=0.70, total_tokens=200_000
    )
    heavy = await _seed_finalized(
        maker, miner=_MINERS[2], composite=0.60, total_tokens=400_000
    )
    return {"lean": lean, "mid": mid, "heavy": heavy}


def _entry(payload: dict, agent_id: UUID) -> dict:
    for entry in payload["entries"]:
        if entry["agent_id"] == str(agent_id):
            return entry
    raise AssertionError(f"agent {agent_id} not on the board")


class TestLeaderboardBonusExposure:
    async def test_active_cohort_awards_frozen_bonuses(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            response = await client.get("/api/v1/public/leaderboard")
        assert response.status_code == 200
        payload = response.json()

        status = payload["efficiency"]
        assert status is not None
        assert status["active"] is True
        assert status["bench_version"] == 7
        assert status["run_size"] == "full"
        assert status["n_min"] == 3
        assert status["cohort_size"] == 3
        assert status["bonus_cap"] == 0.05
        # 3 members at 100k/200k/400k: nearest-rank P25 = 100k, median = 200k.
        assert status["reference_p25_tokens"] == 100_000.0
        assert status["reference_median_tokens"] == 200_000.0
        # Two-tier policy frozen with its knobs and derived deep frontier.
        assert status["curve_version"] == 2
        assert status["deep_bonus_cap"] == 0.10
        assert status["deep_frontier_tokens"] == 50_000.0

        lean = _entry(payload, agents["lean"])
        assert lean["efficiency_bonus"] == 0.05
        assert lean["effective_composite"] == pytest.approx(0.80 * 1.05)
        assert "efficiency_snapshot_id" not in lean
        assert lean["composite"] == 0.80  # base composite is never modified

        mid = _entry(payload, agents["mid"])
        assert mid["efficiency_bonus"] == 0.0
        assert mid["effective_composite"] == pytest.approx(0.70)

        heavy = _entry(payload, agents["heavy"])
        assert heavy["efficiency_bonus"] == 0.0
        assert heavy["effective_composite"] == pytest.approx(0.60)

        # Ranking still follows the base composite (fold wiring is flag-off).
        assert [e["agent_id"] for e in payload["entries"]] == [
            str(agents["lean"]),
            str(agents["mid"]),
            str(agents["heavy"]),
        ]

    async def test_fold_ranks_on_continual_score_times_bonus(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _activate_bench_version(session_maker, 7)
        expensive_leader = await _seed_finalized(
            session_maker,
            miner=_MINERS[0],
            composite=0.80,
            total_tokens=400_000,
        )
        efficient_challenger = await _seed_finalized(
            session_maker,
            miner=_MINERS[1],
            composite=0.79,
            total_tokens=50_000,
        )
        await _seed_finalized(
            session_maker,
            miner=_MINERS[2],
            composite=0.60,
            total_tokens=200_000,
        )
        fold_on = EfficiencyBonusConfig(enabled=True, fold_enabled=True, min_cohort=3)
        app = _make_app(session_maker, efficiency=fold_on)

        async with _client(app) as client:
            response = await client.get("/api/v1/public/leaderboard")

        assert response.status_code == 200
        payload = response.json()
        leader = payload["entries"][0]
        assert leader["agent_id"] == str(efficient_challenger)
        assert leader["pre_efficiency_composite"] == pytest.approx(0.79)
        assert leader["efficiency_bonus"] == pytest.approx(0.05)
        assert leader["official_composite"] == pytest.approx(0.79 * 1.05)
        assert leader["effective_composite"] == pytest.approx(0.79 * 1.05)
        assert _entry(payload, expensive_leader)["rank"] == 2

    async def test_bonuses_and_reference_are_frozen_within_an_epoch(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            first = (await client.get("/api/v1/public/leaderboard")).json()

            # A cheaper newcomer finalizes mid-epoch. It must be scored against
            # the FROZEN reference; nothing already published may move.
            newcomer = await _seed_finalized(
                session_maker,
                miner=_MINERS[3],
                composite=0.75,
                total_tokens=50_000,
            )
            second = (await client.get("/api/v1/public/leaderboard")).json()

        assert second["efficiency"]["snapshot_id"] == first["efficiency"]["snapshot_id"]
        assert second["efficiency"]["reference_p25_tokens"] == 100_000.0
        assert second["efficiency"]["reference_median_tokens"] == 200_000.0
        for name in ("lean", "mid", "heavy"):
            assert (
                _entry(second, agents[name])["efficiency_bonus"]
                == _entry(first, agents[name])["efficiency_bonus"]
            )
        # 50k == the frozen deep frontier (0.5 x P25 = 50k) -> the newcomer
        # saturates at the frozen deep cap, judged against the frozen policy.
        assert _entry(second, newcomer)["efficiency_bonus"] == 0.10
        assert _entry(second, newcomer)["effective_composite"] == pytest.approx(
            0.75 * 1.10
        )

    async def test_cohort_member_below_deep_frontier_saturates_at_deep_cap(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Full two-tier epoch: five cohort members whose totals put one below
        the deep frontier (flat deep cap), one exactly on P25 (base cap), and
        the rest at or past the median (zero)."""
        await _activate_bench_version(session_maker, 7)
        totals = [10_000, 100_000, 110_000, 200_000, 400_000]
        agents = [
            await _seed_finalized(
                session_maker,
                miner=miner,
                composite=0.9 - i * 0.05,
                total_tokens=total,
            )
            for i, (miner, total) in enumerate(zip(_MINERS, totals, strict=True))
        ]
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            payload = (await client.get("/api/v1/public/leaderboard")).json()

        status = payload["efficiency"]
        # n=5: nearest-rank P25 = 2nd smallest = 100k; median = 110k;
        # deep frontier = 0.5 x 100k = 50k.
        assert status["active"] is True
        assert status["reference_p25_tokens"] == 100_000.0
        assert status["reference_median_tokens"] == 110_000.0
        assert status["deep_frontier_tokens"] == 50_000.0

        deep, at_p25, at_median, heavy, heaviest = (
            _entry(payload, agent_id) for agent_id in agents
        )
        assert deep["efficiency_bonus"] == 0.10  # 10k < 50k: saturated
        assert deep["effective_composite"] == pytest.approx(0.9 * 1.10)
        assert at_p25["efficiency_bonus"] == 0.05  # exactly P25: base cap
        assert at_median["efficiency_bonus"] == 0.0  # at the median: zero
        assert heavy["efficiency_bonus"] == 0.0
        assert heaviest["efficiency_bonus"] == 0.0

    async def test_below_n_min_is_inactive_and_awards_nothing(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _activate_bench_version(session_maker, 7)
        a = await _seed_finalized(
            session_maker, miner=_MINERS[0], composite=0.8, total_tokens=100_000
        )
        b = await _seed_finalized(
            session_maker, miner=_MINERS[1], composite=0.7, total_tokens=200_000
        )
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            payload = (await client.get("/api/v1/public/leaderboard")).json()

        status = payload["efficiency"]
        assert status is not None
        assert status["active"] is False
        assert status["cohort_size"] == 2
        assert status["reference_p25_tokens"] is None
        assert status["reference_median_tokens"] is None
        for agent_id in (a, b):
            entry = _entry(payload, agent_id)
            assert entry["efficiency_bonus"] is None
            assert entry["effective_composite"] is None
            assert "efficiency_snapshot_id" not in entry
        # Inactive epochs assign no rows at all — activation later must be
        # able to freeze these agents at their first ACTIVE epoch.
        async with session_maker() as s:
            assert (await s.scalars(select(EfficiencyBonus))).all() == []

    async def test_lineage_dedupe_collapses_copies_before_the_frontier(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _activate_bench_version(session_maker, 7)
        original = await _seed_finalized(
            session_maker,
            miner=_MINERS[0],
            composite=0.85,
            total_tokens=100_000,
            sha256="cc" * 32,
            created_at=_T0,
        )
        copycat = await _seed_finalized(
            session_maker,
            miner=_MINERS[1],
            composite=0.80,
            total_tokens=100_000,
            sha256="cc" * 32,  # byte-identical artifact under another hotkey
            created_at=_T0 + timedelta(hours=1),
        )
        others = [
            await _seed_finalized(
                session_maker,
                miner=miner,
                composite=0.7 - i * 0.05,
                total_tokens=200_000 + i * 100_000,
            )
            for i, miner in enumerate(_MINERS[2:4])
        ]
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            board = (await client.get("/api/v1/public/leaderboard")).json()
            snapshot_id = board["efficiency"]["snapshot_id"]
            snapshot = (
                await client.get(f"/api/v1/public/efficiency/snapshots/{snapshot_id}")
            ).json()

        # 4 submissions, 3 lineages: the duplicate collapsed into the original.
        assert board["efficiency"]["cohort_size"] == 3
        members = {member["agent_id"]: member for member in snapshot["members"]}
        assert str(original) in members
        assert str(copycat) not in members
        assert members[str(original)]["collapsed_agent_ids"] == [str(copycat)]
        for other in others:
            assert str(other) in members
        # No raw lineage digests on the public wire — opaque ordinals only.
        assert "lineage_key" not in next(iter(members.values()))

    async def test_pre_v7_board_is_untouched(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A board on a retired era stays byte-identical with the flag ON.

        The bonus is a v7 feature, so the only way to prove it never engages
        below v7 is to serve a board built from genuinely sub-v7 scores. The
        bench-version floor refuses to write those, which is exactly the
        production situation: the v2 rows are grandfathered by a ``NOT VALID``
        constraint and are still read back by the leaderboard. The helper
        reproduces that state rather than pretending v2 rows no longer exist.
        """
        # Default active version (2): flag ON but the bonus must never engage.
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            for i, miner in enumerate(_MINERS[:3]):
                await _seed_finalized(
                    session_maker,
                    miner=miner,
                    composite=0.8 - i * 0.1,
                    total_tokens=100_000 * (i + 1),
                    bench_version=2,
                )
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            payload = (await client.get("/api/v1/public/leaderboard")).json()

        assert payload["efficiency"] is None
        for entry in payload["entries"]:
            assert entry["efficiency_bonus"] is None
            assert entry["effective_composite"] is None
            assert "efficiency_snapshot_id" not in entry
        async with session_maker() as s:
            assert (await s.scalars(select(EfficiencyCohortSnapshot))).all() == []
            assert (await s.scalars(select(EfficiencyBonus))).all() == []

    async def test_disabled_flag_previews_without_writing_or_applying(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Switched off must mean "visible but not applied", not "invisible".

        The block used to disappear entirely when the bonus was off, so the only
        way to see the boost was to enable it -- and enabling WRITES: `enabled`
        gates ensure_efficiency_state, which freezes a snapshot and inserts a
        bonus row per agent. That is how rows got frozen at 04:43Z part-way
        through the token-budget transition.

        So the disabled board previews instead: the same arithmetic, computed at
        read time, persisted nowhere, applied to nothing.
        """
        agents = await _seed_v7_board(session_maker)
        app = _make_app(session_maker, efficiency=_DISABLED)
        async with _client(app) as client:
            payload = (await client.get("/api/v1/public/leaderboard")).json()

        efficiency = payload["efficiency"]
        assert efficiency is not None
        assert efficiency["preview"] is True
        # A preview is never active and has no snapshot to resolve, because it
        # froze nothing.
        assert efficiency["active"] is False
        assert efficiency["snapshot_id"] is None
        assert efficiency["reference_p25_tokens"] is not None

        lean = _entry(payload, agents["lean"])
        # The "would be" number is real arithmetic on a separate field...
        assert lean["efficiency_bonus_preview"] is not None
        assert lean["efficiency_bonus_preview"] > 0.0
        # ...and the applied fields stay null, so no consumer can mistake an
        # unapplied preview for an awarded bonus.
        assert lean["efficiency_bonus"] is None
        assert lean["effective_composite"] is None
        assert "efficiency_snapshot_id" not in lean

        # The whole point: nothing was persisted.
        async with session_maker() as s:
            assert (await s.scalars(select(EfficiencyCohortSnapshot))).all() == []
            assert (await s.scalars(select(EfficiencyBonus))).all() == []

    async def test_a_preview_never_reaches_the_validator_ledger(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """fold_effective = enabled AND fold_enabled, and a preview is neither.

        The preview exists to be looked at. If it could reach the ledger it
        would be a scoring change wearing an observability costume.
        """
        from ditto.api_server.efficiency import preview_efficiency_board

        await _seed_v7_board(session_maker)
        config = _DISABLED
        assert config.enabled is False
        assert config.fold_enabled is False

        async with session_maker() as s:
            view = await preview_efficiency_board(s, config, bench_version=7, now=_T0)

        assert view is not None
        assert view.preview is True
        # `bonuses` is what every fold-facing reader consumes; a preview leaves
        # it empty and puts its numbers in a field no fold path reads.
        assert view.bonuses == {}
        assert view.preview_bonuses

    async def test_snapshot_endpoint_404_for_unknown_id(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        app = _make_app(session_maker, efficiency=_ENABLED)
        async with _client(app) as client:
            response = await client.get(
                f"/api/v1/public/efficiency/snapshots/{uuid4()}"
            )
        assert response.status_code == 404


class TestEpochFreezing:
    async def test_new_epoch_freezes_a_new_snapshot_without_mutating_the_old(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        async with session_maker() as s:
            await ensure_efficiency_state(s, _ENABLED, now=_T0)
        async with session_maker() as s:
            snapshots = (await s.scalars(select(EfficiencyCohortSnapshot))).all()
            assert len(snapshots) == 1
            first_id = snapshots[0].snapshot_id
            first_epoch = snapshots[0].epoch_index
            first_members = list(snapshots[0].members or [])
            first_p25 = snapshots[0].reference_p25_tokens

        # A leaner agent lands in the NEXT epoch: fresh cohort, fresh frontier.
        newcomer = await _seed_finalized(
            session_maker,
            miner=_MINERS[3],
            composite=0.75,
            total_tokens=50_000,
        )
        async with session_maker() as s:
            await ensure_efficiency_state(s, _ENABLED, now=_T0 + timedelta(hours=25))

        async with session_maker() as s:
            snapshots = (
                await s.scalars(
                    select(EfficiencyCohortSnapshot).order_by(
                        EfficiencyCohortSnapshot.epoch_index
                    )
                )
            ).all()
            assert len(snapshots) == 2
            old, new = snapshots
            # The historical snapshot did not move.
            assert old.snapshot_id == first_id
            assert old.epoch_index == first_epoch
            assert list(old.members or []) == first_members
            assert old.reference_p25_tokens == first_p25
            # The new one reflects the new population under the ratcheted
            # quality floor (median of the previous cohort = 0.7, so the 0.6
            # agent drops out and the 0.75 newcomer joins).
            assert new.epoch_index == first_epoch + 1
            assert new.quality_floor == 0.7
            assert len(new.members or []) == 3
            assert new.reference_p25_tokens == 50_000.0

            bonuses = {
                (row.agent_id, row.epoch_index): row
                for row in (await s.scalars(select(EfficiencyBonus))).all()
            }
        # Every agent's epoch-1 row is still there, still pointing at snapshot 1,
        # still holding the value it was published with. Recomputation is
        # additive; it never rewrites history.
        for agent_id in agents.values():
            frozen = bonuses[(agent_id, first_epoch)]
            assert frozen.snapshot_id == first_id

        # ...and epoch 2 recomputed against snapshot 2. This is the fix: before
        # epoch_index joined the key, `_materialize_epoch` skipped any agent
        # already present, so an agent measured in epoch 1 kept that bonus for
        # the life of the bench version no matter how its efficiency changed.
        # Only the two agents still clearing the ratcheted 0.7 quality floor are
        # in the epoch-2 cohort; the 0.6 agent dropped out of it.
        recomputed = [
            agent_id
            for agent_id in agents.values()
            if (agent_id, new.epoch_index) in bonuses
        ]
        assert recomputed, "epoch 2 must reassign, not inherit epoch 1"
        for agent_id in recomputed:
            assert bonuses[(agent_id, new.epoch_index)].snapshot_id == new.snapshot_id

        assert bonuses[(newcomer, new.epoch_index)].snapshot_id == new.snapshot_id
        assert bonuses[(newcomer, new.epoch_index)].bonus == 0.05

    async def test_quality_floors_ratchet_from_previous_active_cohort(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_v7_board(session_maker)  # composites 0.8 / 0.7 / 0.6
        async with session_maker() as s:
            await ensure_efficiency_state(s, _ENABLED, now=_T0)
        async with session_maker() as s:
            await ensure_efficiency_state(s, _ENABLED, now=_T0 + timedelta(hours=25))
        async with session_maker() as s:
            snapshots = (
                await s.scalars(
                    select(EfficiencyCohortSnapshot).order_by(
                        EfficiencyCohortSnapshot.epoch_index
                    )
                )
            ).all()
        assert snapshots[0].quality_floor == 0.0
        # Epoch 2: Q_min = previous cohort median composite (0.7),
        # M_min = 0.8 x previous median memory_mean (0.8 x 0.7).
        assert snapshots[1].quality_floor == 0.7
        assert snapshots[1].memory_floor == pytest.approx(0.8 * 0.7)
        # The 0.6 agent no longer qualifies; cohort shrinks below n_min=3.
        assert snapshots[1].active is False


class TestValidatorLedgerFoldFlag:
    def _headers(self) -> dict[str, str]:
        nonce = uuid4()
        requested_at = datetime.now(UTC)
        requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
        signed = (
            f"validator-ledger:v1:{_VALIDATOR_HOTKEY}:{nonce}:{requested}"
        ).encode()
        return {
            "X-Validator-Hotkey": _VALIDATOR_HOTKEY,
            "X-Validator-Ledger-Nonce": str(nonce),
            "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
            "X-Validator-Ledger-Signature": _KEYPAIR.sign(signed).hex(),
        }

    def _install_chain(self, app: FastAPI) -> None:
        from unittest.mock import AsyncMock, MagicMock

        async def _chain() -> MagicMock:
            c = MagicMock()
            c.get_recent_neurons = AsyncMock(
                return_value=[
                    NeuronInfo(
                        hotkey=_VALIDATOR_HOTKEY,
                        coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                        uid=1,
                        stake=1000.0,
                        validator_permit=True,
                    )
                ]
            )
            return c

        app.dependency_overrides[get_chain_client] = _chain

    async def _ledger(
        self,
        maker: async_sessionmaker[AsyncSession],
        efficiency: EfficiencyBonusConfig,
    ) -> dict:
        app = _make_app(maker, efficiency=efficiency)
        self._install_chain(app)
        async with _client(app) as client:
            response = await client.get(
                "/api/v1/scoring/scores", headers=self._headers()
            )
        assert response.status_code == 200
        return response.json()

    async def test_fold_flag_off_keeps_ledger_fields_null(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _seed_v7_board(session_maker)
        async with session_maker() as s:
            await ensure_efficiency_state(s, _ENABLED, now=_T0)

        payload = await self._ledger(session_maker, _ENABLED)
        assert payload["count"] == 3
        for entry in payload["entries"]:
            assert entry["efficiency_bonus"] is None
            assert entry["effective_composite"] is None

    async def test_fold_flag_on_exposes_frozen_effective_composite(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        agents = await _seed_v7_board(session_maker)
        fold_on = EfficiencyBonusConfig(enabled=True, fold_enabled=True, min_cohort=3)

        # The validator ledger is itself an epoch materializer; no public page
        # visit or test-side pre-materialization is required for consensus data.
        payload = await self._ledger(session_maker, fold_on)
        by_agent = {entry["agent_id"]: entry for entry in payload["entries"]}
        lean = by_agent[str(agents["lean"])]
        assert lean["efficiency_bonus"] == 0.05
        assert lean["composite"] == 0.80
        assert lean["effective_composite"] == pytest.approx(0.80 * 1.05)
        heavy = by_agent[str(agents["heavy"])]
        assert heavy["efficiency_bonus"] == 0.0
        assert heavy["effective_composite"] == pytest.approx(0.60)

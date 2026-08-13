"""Unit tests for :mod:`ditto.db.queries.efficiency` against SQLite-in-memory.

The two tables are append-only by contract: snapshots are unique per
``(bench_version, run_size, epoch)`` and bonus rows insert-once per
``(agent_id, bench_version)`` — a duplicate insert must fail loudly rather
than silently mutate frozen history.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.config import EfficiencyBonusConfig
from ditto.api_server.efficiency import (
    CURVE_VERSION_BOUNDED_FACTOR,
    CohortMember,
    CohortReference,
    _finalized_ranked_rows,
    ensure_efficiency_state,
    epoch_index_for,
    preview_efficiency_board,
    reference_from_snapshot,
)
from ditto.db.models import Agent, ConfirmationScore, EfficiencyCohortSnapshot
from ditto.db.queries.efficiency import (
    get_bonus_rows,
    get_snapshot,
    get_snapshot_by_id,
    insert_bonus,
    insert_snapshot,
    latest_snapshot,
    promote_v3_compatibility_placeholder,
)
from ditto_screening_protocol.bench_v9 import V9BaseEvidence, V9ScoreGateEvidence

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_V9_VECTOR = (
    Path(__file__).resolve().parents[6]
    / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
)


def _v9_details(total: int) -> dict:
    raw = copy.deepcopy(json.loads(_V9_VECTOR.read_text())["vectors"][0]["details"])
    # The vector has three eligible cases and four successful requests, so
    # callers use totals >=1,200 to satisfy the frozen v3 integrity floors.
    raw["score_gates"]["model_use"]["prompt_tokens"] = total
    raw["score_gates"]["model_use"]["completion_tokens"] = 0
    gates = V9ScoreGateEvidence.model_validate(raw["score_gates"])
    raw["score_gates_sha256"] = gates.digest_hex()
    evidence = V9BaseEvidence.model_validate(raw)
    return {
        "v9_base": raw,
        "base_evidence_sha256": evidence.digest_hex(),
    }


def _reference(
    *,
    epoch_index: int = 1000,
    active: bool = True,
    members: tuple[CohortMember, ...] = (),
) -> CohortReference:
    return CohortReference(
        bench_version=7,
        run_size="full",
        epoch_index=epoch_index,
        active=active,
        cohort_limit=25,
        n_min=8,
        bonus_cap=0.05,
        quality_floor=0.5,
        memory_floor=0.4,
        reference_p25_tokens=100.0 if active else None,
        reference_median_tokens=200.0 if active else None,
        members=members,
    )


def _member(
    n: int,
    *,
    collapsed: tuple[UUID, ...] = (),
    composite: float = 0.8,
    memory_mean: float = 0.7,
) -> CohortMember:
    return CohortMember(
        agent_id=UUID(int=n),
        miner_hotkey=_MINER,
        lineage_key=f"sha:{n:064x}",
        composite=composite,
        memory_mean=memory_mean,
        token_total=100.0 + n,
        collapsed_agent_ids=collapsed,
        first_seen=datetime(2026, 7, 24, 12, n, tzinfo=UTC),
    )


async def _seed_agent(session: AsyncSession) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=_MINER,
        name="alpha",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        created_at=datetime.now(UTC),
    )
    async with session.begin():
        session.add(agent)
    return agent


async def _insert_historical_snapshot(
    session: AsyncSession, reference: CohortReference
) -> EfficiencyCohortSnapshot:
    """Seed a pre-migration v9 snapshot while preserving the live write guard."""
    await session.execute(
        text(
            "ALTER TABLE efficiency_cohort_snapshots DISABLE TRIGGER "
            "efficiency_cohort_snapshots_curve_guard"
        )
    )
    try:
        return await insert_snapshot(session, reference)
    finally:
        await session.execute(
            text(
                "ALTER TABLE efficiency_cohort_snapshots ENABLE TRIGGER "
                "efficiency_cohort_snapshots_curve_guard"
            )
        )


class TestSnapshots:
    async def test_roundtrip_including_members_json(self, session: AsyncSession):
        reference = _reference(
            members=(_member(1, collapsed=(UUID(int=9),)), _member(2))
        )
        async with session.begin():
            inserted = await insert_snapshot(session, reference)

        read = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=1000
        )
        assert read is not None
        assert read.snapshot_id == inserted.snapshot_id
        assert read.active is True
        assert read.reference_p25_tokens == 100.0
        assert read.reference_median_tokens == 200.0
        assert read.members is not None and len(read.members) == 2
        assert read.members[0]["agent_id"] == str(UUID(int=1))
        assert read.members[0]["collapsed_agent_ids"] == [str(UUID(int=9))]
        assert read.members[0]["first_seen"] == "2026-07-24T12:01:00+00:00"
        # Default reference = the single-tier legacy policy.
        assert read.curve_version == 1
        assert read.deep_bonus_cap is None
        assert read.deep_frontier_ratio is None
        assert read.factor_alpha is None
        assert read.minimum_factor is None
        assert read.maximum_factor is None
        assert await get_snapshot_by_id(session, inserted.snapshot_id) is not None

    async def test_legacy_member_without_first_seen_still_rehydrates(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            await insert_snapshot(session, _reference(members=(_member(1),)))
        stored = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=1000
        )
        assert stored is not None and stored.members is not None
        legacy_members = copy.deepcopy(stored.members)
        legacy_members[0].pop("first_seen")
        stored.members = legacy_members

        rehydrated = reference_from_snapshot(stored)
        assert rehydrated.members[0].agent_id == UUID(int=1)
        assert rehydrated.members[0].first_seen is None

    async def test_two_tier_policy_roundtrip(self, session: AsyncSession):
        from dataclasses import replace

        reference = replace(
            _reference(),
            curve_version=2,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
        )
        async with session.begin():
            await insert_snapshot(session, reference)
        read = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=1000
        )
        assert read is not None
        assert read.curve_version == 2
        assert read.deep_bonus_cap == 0.10
        assert read.deep_frontier_ratio == 0.5

    async def test_bounded_factor_policy_roundtrip(self, session: AsyncSession):
        from dataclasses import replace

        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        async with session.begin():
            await insert_snapshot(session, reference)
        read = await get_snapshot(
            session, bench_version=9, run_size="full", epoch_index=1000
        )
        assert read is not None
        assert read.curve_version == 3
        assert read.factor_alpha == 0.25
        assert read.minimum_factor == 0.85
        assert read.maximum_factor == 1.10

    async def test_curve_three_rejects_legacy_deep_knobs(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        invalid = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_snapshot(session, invalid)

    async def test_legacy_curve_rejects_factor_knobs(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        invalid = replace(
            _reference(),
            curve_version=2,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
            factor_alpha=0.25,
        )
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_snapshot(session, invalid)

    async def test_pre_tier_snapshot_reproduces_single_tier_bonuses(
        self, session: AsyncSession
    ):
        """A stored curve_version-1 snapshot must reproduce its original
        single-tier bonuses via ``reference_from_snapshot`` forever, even on a
        build whose config defaults to the two-tier curve."""
        from ditto.api_server.efficiency import (
            bonus_for_submission,
            reference_from_snapshot,
        )

        async with session.begin():
            await insert_snapshot(session, _reference())  # curve_version 1
        stored = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=1000
        )
        assert stored is not None
        rehydrated = reference_from_snapshot(stored)
        assert rehydrated.curve_version == 1
        # P25=100, median=200: deep in what tier 2 would call saturation
        # territory, the frozen single-tier policy still pays exactly the
        # base cap — never the (never-frozen) deep cap.
        assert bonus_for_submission(0.9, 0.9, 10.0, rehydrated) == 0.05
        assert bonus_for_submission(0.9, 0.9, 75.0, rehydrated) == 0.05
        assert bonus_for_submission(0.9, 0.9, 150.0, rehydrated) == 0.025

    async def test_epoch_key_is_unique(self, session: AsyncSession):
        async with session.begin():
            await insert_snapshot(session, _reference())
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_snapshot(session, _reference())

    async def test_new_epoch_never_mutates_the_old_snapshot(
        self, session: AsyncSession
    ):
        async with session.begin():
            first = await insert_snapshot(
                session, _reference(epoch_index=1000, members=(_member(1),))
            )
        original = (
            first.snapshot_id,
            first.reference_p25_tokens,
            first.reference_median_tokens,
            list(first.members or []),
        )

        async with session.begin():
            await insert_snapshot(
                session,
                CohortReference(
                    bench_version=7,
                    run_size="full",
                    epoch_index=1001,
                    active=True,
                    cohort_limit=25,
                    n_min=8,
                    bonus_cap=0.05,
                    quality_floor=0.6,
                    memory_floor=0.5,
                    reference_p25_tokens=50.0,
                    reference_median_tokens=75.0,
                    members=(_member(2), _member(3)),
                ),
            )

        reread = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=1000
        )
        assert reread is not None
        assert (
            reread.snapshot_id,
            reread.reference_p25_tokens,
            reread.reference_median_tokens,
            list(reread.members or []),
        ) == original

    async def test_latest_snapshot_honors_bounds_and_active_filter(
        self, session: AsyncSession
    ):
        async with session.begin():
            await insert_snapshot(session, _reference(epoch_index=1000, active=False))
            newest = await insert_snapshot(session, _reference(epoch_index=1001))

        found = await latest_snapshot(
            session,
            bench_version=7,
            run_size="full",
            max_epoch_index=1001,
        )
        assert found is not None and found.snapshot_id == newest.snapshot_id

        bounded = await latest_snapshot(
            session,
            bench_version=7,
            run_size="full",
            max_epoch_index=1000,
        )
        assert bounded is not None and bounded.epoch_index == 1000

        active_only = await latest_snapshot(
            session,
            bench_version=7,
            run_size="full",
            max_epoch_index=1000,
            active_only=True,
        )
        assert active_only is None

    async def test_missing_snapshot_reads_return_none(self, session: AsyncSession):
        assert (
            await get_snapshot(session, bench_version=7, run_size="full", epoch_index=1)
            is None
        )
        assert (
            await latest_snapshot(
                session, bench_version=7, run_size="full", max_epoch_index=10
            )
            is None
        )


class TestBonuses:
    async def test_insert_once_and_read_back(self, session: AsyncSession):
        agent = await _seed_agent(session)
        async with session.begin():
            snapshot = await insert_snapshot(session, _reference(epoch_index=1))
            await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=7,
                epoch_index=1,
                snapshot_id=snapshot.snapshot_id,
                token_total=90.0,
                bonus=0.05,
            )

        rows = await get_bonus_rows(
            session, [agent.agent_id], bench_versions={agent.agent_id: 7}
        )
        assert rows[agent.agent_id].bonus == 0.05
        assert rows[agent.agent_id].snapshot_id == snapshot.snapshot_id
        assert rows[agent.agent_id].token_total == 90.0
        assert rows[agent.agent_id].factor is None

    async def test_bounded_factor_insert_and_read_back(self, session: AsyncSession):
        from dataclasses import replace

        agent = await _seed_agent(session)
        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        async with session.begin():
            snapshot = await insert_snapshot(session, reference)
            await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=9,
                epoch_index=1000,
                snapshot_id=snapshot.snapshot_id,
                token_total=90.0,
                bonus=0.0,
                factor=1.10,
            )

        rows = await get_bonus_rows(
            session,
            [agent.agent_id],
            bench_versions={agent.agent_id: 9},
            epoch_index=1000,
        )
        assert rows[agent.agent_id].bonus == 0.0
        assert rows[agent.agent_id].factor == 1.10

    async def test_bounded_factor_rejects_nonzero_legacy_bonus(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        agent = await _seed_agent(session)
        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                snapshot = await insert_snapshot(session, reference)
                await insert_bonus(
                    session,
                    agent_id=agent.agent_id,
                    bench_version=9,
                    epoch_index=1000,
                    snapshot_id=snapshot.snapshot_id,
                    token_total=90.0,
                    bonus=0.01,
                    factor=1.10,
                )

    async def test_previous_binary_write_is_neutral_then_promotable(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        agent = await _seed_agent(session)
        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        agent_id = agent.agent_id
        async with session.begin():
            snapshot = await insert_snapshot(session, reference)
            placeholder = await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=9,
                epoch_index=1000,
                snapshot_id=snapshot.snapshot_id,
                token_total=90.0,
                # Exact previous-binary shape: it omits factor and may compute
                # a positive v2 bonus. The DB must force it neutral.
                bonus=0.05,
                factor=None,
            )
            await session.refresh(placeholder)
            assert placeholder.bonus == 0.0
            assert placeholder.factor is None

            await promote_v3_compatibility_placeholder(
                session, placeholder, token_total=80.0, factor=1.10
            )
            assert placeholder.token_total == 80.0
            assert placeholder.factor == 1.10

        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE efficiency_cohort_snapshots "
                        "SET reference_p25_tokens = 1 "
                        "WHERE snapshot_id = :snapshot_id"
                    ),
                    {"snapshot_id": snapshot.snapshot_id},
                )

        # Promotion is the only compatibility UPDATE. Once scoring authority
        # exists, neither the factor nor the evidence it was derived from may
        # drift, even through raw SQL outside the insert-only query layer.
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE efficiency_bonuses "
                        "SET factor = 0.85, token_total = 999 "
                        "WHERE agent_id = :agent_id "
                        "AND bench_version = 9 AND epoch_index = 1000"
                    ),
                    {"agent_id": agent_id},
                )

        rows = await get_bonus_rows(
            session,
            [agent_id],
            bench_versions={agent_id: 9},
            epoch_index=1000,
        )
        assert rows[agent_id].factor == 1.10
        assert rows[agent_id].token_total == 80.0

    async def test_factor_requires_positive_finite_token_authority(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        agent = await _seed_agent(session)
        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                snapshot = await insert_snapshot(session, reference)
                await insert_bonus(
                    session,
                    agent_id=agent.agent_id,
                    bench_version=9,
                    epoch_index=1000,
                    snapshot_id=snapshot.snapshot_id,
                    token_total=None,
                    bonus=0.0,
                    factor=1.10,
                )

    async def test_factor_cannot_attach_to_legacy_snapshot(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        agent = await _seed_agent(session)
        legacy_v9 = replace(
            _reference(epoch_index=999),
            bench_version=9,
            curve_version=2,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
        )
        async with session.begin():
            snapshot = await _insert_historical_snapshot(session, legacy_v9)
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_bonus(
                    session,
                    agent_id=agent.agent_id,
                    bench_version=9,
                    epoch_index=999,
                    snapshot_id=snapshot.snapshot_id,
                    token_total=90.0,
                    bonus=0.0,
                    factor=1.10,
                )

    async def test_historical_legacy_snapshot_still_replays_but_new_one_is_rejected(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        historical = replace(
            _reference(epoch_index=999),
            bench_version=9,
            curve_version=2,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
        )
        async with session.begin():
            inserted = await _insert_historical_snapshot(session, historical)
        assert inserted.curve_version == 2

        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_snapshot(session, replace(historical, epoch_index=1000))

    async def test_assignment_must_match_snapshot_benchmark_and_epoch(
        self, session: AsyncSession
    ) -> None:
        from dataclasses import replace

        agent = await _seed_agent(session)
        reference = replace(
            _reference(),
            bench_version=9,
            curve_version=3,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                snapshot = await insert_snapshot(session, reference)
                await insert_bonus(
                    session,
                    agent_id=agent.agent_id,
                    bench_version=9,
                    epoch_index=1001,
                    snapshot_id=snapshot.snapshot_id,
                    token_total=90.0,
                    bonus=0.0,
                    factor=1.10,
                )

    async def test_duplicate_assignment_is_rejected(self, session: AsyncSession):
        agent = await _seed_agent(session)
        # Primitives captured up front: the failed transaction's rollback
        # expires every ORM object, so attribute access after it would lazy-load.
        agent_id = agent.agent_id
        async with session.begin():
            snapshot = await insert_snapshot(session, _reference(epoch_index=1))
            snapshot_id = snapshot.snapshot_id
            await insert_bonus(
                session,
                agent_id=agent_id,
                bench_version=7,
                epoch_index=1,
                snapshot_id=snapshot_id,
                token_total=90.0,
                bonus=0.05,
            )
        # Same epoch -> still rejected. Immutability within an epoch is exactly
        # what keeps a published effective score from drifting.
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await insert_bonus(
                    session,
                    agent_id=agent_id,
                    bench_version=7,
                    epoch_index=1,
                    snapshot_id=snapshot_id,
                    token_total=90.0,
                    bonus=0.01,
                )
        rows = await get_bonus_rows(session, [agent_id], bench_versions={agent_id: 7})
        assert rows[agent_id].bonus == 0.05

    async def test_a_later_epoch_recomputes_beside_the_frozen_row(
        self, session: AsyncSession
    ):
        """The freeze fix: a new epoch adds a row, it never mutates the old one.

        Before ``epoch_index`` joined the key, an agent's bonus was insert-once
        per bench version FOREVER -- so an agent that became more efficient could
        never earn a better one, and an agent measured mid-transition kept that
        measurement for the life of the contract.
        """
        agent = await _seed_agent(session)
        async with session.begin():
            first = await insert_snapshot(session, _reference(epoch_index=1))
            await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=7,
                epoch_index=1,
                snapshot_id=first.snapshot_id,
                token_total=90.0,
                bonus=0.02,
            )
        async with session.begin():
            second = await insert_snapshot(session, _reference(epoch_index=2))
            await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=7,
                epoch_index=2,
                snapshot_id=second.snapshot_id,
                token_total=40.0,
                bonus=0.09,
            )

        # The scoring path names its epoch, so it reads the current one.
        current = await get_bonus_rows(
            session,
            [agent.agent_id],
            bench_versions={agent.agent_id: 7},
            epoch_index=2,
        )
        assert current[agent.agent_id].bonus == 0.09

        # And the earlier epoch is untouched, so its published snapshot still
        # reproduces exactly. Nothing had to be deleted for the agent to move on.
        historical = await get_bonus_rows(
            session,
            [agent.agent_id],
            bench_versions={agent.agent_id: 7},
            epoch_index=1,
        )
        assert historical[agent.agent_id].bonus == 0.02
        assert historical[agent.agent_id].snapshot_id == first.snapshot_id

    async def test_version_scoped_read(self, session: AsyncSession):
        agent = await _seed_agent(session)
        async with session.begin():
            snapshot = await insert_snapshot(session, _reference(epoch_index=1))
            await insert_bonus(
                session,
                agent_id=agent.agent_id,
                bench_version=7,
                epoch_index=1,
                snapshot_id=snapshot.snapshot_id,
                token_total=90.0,
                bonus=0.02,
            )
        assert (
            await get_bonus_rows(
                session, [agent.agent_id], bench_versions={agent.agent_id: 8}
            )
            == {}
        )
        assert await get_bonus_rows(session, [], bench_versions={}) == {}


class TestBoundedFactorMaterialization:
    async def test_finalized_rows_load_normalized_hash_for_lineage_dedupe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        normalized = "cd" * 32
        row = SimpleNamespace(
            agent_id=uuid4(),
            bench_version=9,
            eligible=True,
            normalized_source_hash=normalized,
        )

        async def ledger(_session: object, **kwargs: object):
            assert kwargs["include_fingerprints"] is True
            assert kwargs["include_details"] is False
            assert kwargs["dedupe_owners"] is False
            return [row]

        async def counts(
            _session: object,
            agent_ids: list[UUID],
            *,
            bench_versions: dict[UUID, int],
        ) -> dict[UUID, int]:
            assert agent_ids == [row.agent_id]
            assert bench_versions == {row.agent_id: 9}
            return {row.agent_id: 3}

        monkeypatch.setattr("ditto.db.queries.scores.list_eligible_ledger", ledger)
        monkeypatch.setattr("ditto.db.queries.scores.get_score_counts", counts)

        loaded = await _finalized_ranked_rows(object())  # type: ignore[arg-type]
        assert loaded == [row]
        assert loaded[0].normalized_source_hash == normalized

    @staticmethod
    def _patch_v9_board(
        monkeypatch: pytest.MonkeyPatch,
        agents: list[Agent],
        *,
        now: datetime,
        quality: float = 0.8,
        memory_mean: float = 0.8,
        cost: int = 10_000,
    ) -> None:
        rows = [
            SimpleNamespace(
                agent_id=agent.agent_id,
                miner_hotkey=agent.miner_hotkey,
                normalized_source_hash=None,
                sha256=f"{index + 1:064x}",
                composite=quality,
                memory_mean=memory_mean,
                first_seen=now,
                bench_version=9,
                v9_confirmation={"full_effective_micros": int(quality * 1_000_000)},
            )
            for index, agent in enumerate(agents)
        ]
        score_rows = {
            agent.agent_id: [
                SimpleNamespace(details=_v9_details(cost)),
                SimpleNamespace(details=_v9_details(cost)),
                SimpleNamespace(details=_v9_details(cost)),
            ]
            for agent in agents
        }

        async def finalized(_session: AsyncSession):
            return rows

        async def quorum(_session, agent_ids, *, bench_versions):
            del agent_ids, bench_versions
            return score_rows

        monkeypatch.setattr(
            "ditto.api_server.efficiency._finalized_ranked_rows", finalized
        )
        monkeypatch.setattr("ditto.db.queries.scores.quorum_score_rows", quorum)

    @staticmethod
    def _legacy_v2_reference(*, epoch: int) -> CohortReference:
        from dataclasses import replace

        return replace(
            _reference(
                epoch_index=epoch,
                members=tuple(
                    _member(n, composite=0.95, memory_mean=0.95) for n in range(1, 6)
                ),
            ),
            bench_version=9,
            n_min=5,
            curve_version=2,
            deep_bonus_cap=0.10,
            deep_frontier_ratio=0.5,
        )

    async def test_materialization_v3_transition_uses_configured_floors(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        epoch = epoch_index_for(now, 24)
        agents = [await _seed_agent(session) for _ in range(5)]
        async with session.begin():
            await _insert_historical_snapshot(
                session, self._legacy_v2_reference(epoch=epoch - 1)
            )
        self._patch_v9_board(monkeypatch, agents, now=now)
        config = EfficiencyBonusConfig(
            enabled=True,
            min_cohort=5,
            quality_floor=0.5,
            memory_floor=0.4,
        )

        await ensure_efficiency_state(session, config, now=now)

        snapshot = await get_snapshot(
            session, bench_version=9, run_size="full", epoch_index=epoch
        )
        assert snapshot is not None
        assert snapshot.curve_version == CURVE_VERSION_BOUNDED_FACTOR
        assert snapshot.active is True
        assert snapshot.quality_floor == 0.5
        assert snapshot.memory_floor == 0.4

    async def test_preview_v3_transition_uses_configured_floors(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        epoch = epoch_index_for(now, 24)
        agents = [await _seed_agent(session) for _ in range(5)]
        async with session.begin():
            await _insert_historical_snapshot(
                session, self._legacy_v2_reference(epoch=epoch - 1)
            )
        self._patch_v9_board(monkeypatch, agents, now=now)

        view = await preview_efficiency_board(
            session,
            EfficiencyBonusConfig(
                min_cohort=5,
                quality_floor=0.5,
                memory_floor=0.4,
            ),
            bench_version=9,
            now=now,
        )

        assert view is not None and view.preview_reference is not None
        assert view.preview_reference.active is True
        assert view.preview_reference.quality_floor == 0.5
        assert view.preview_reference.memory_floor == 0.4

    async def test_v9_freezes_dynamic_p25_and_factor_rows(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agents = [await _seed_agent(session) for _ in range(5)]
        costs = [5_000, 10_000, 20_000, 30_000, 40_000]
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
        rows = [
            SimpleNamespace(
                agent_id=agent.agent_id,
                miner_hotkey=agent.miner_hotkey,
                normalized_source_hash=None,
                sha256=f"{index + 1:064x}",
                composite=0.8,
                memory_mean=0.8,
                first_seen=now,
                bench_version=9,
                v9_confirmation={"full_effective_micros": 800_000},
            )
            for index, agent in enumerate(agents)
        ]
        score_rows = {
            agent.agent_id: [
                SimpleNamespace(details=_v9_details(cost)),
                SimpleNamespace(details=_v9_details(cost)),
                SimpleNamespace(details=_v9_details(cost)),
            ]
            for agent, cost in zip(agents, costs, strict=True)
        }
        # Protocol-19 continual evidence joins the canonical roots per SEED,
        # not per row. The three quorum receipts re-score one pinned seed and
        # median to a single 5,000 observation; the retest seed adds 15,000.
        # median([5,000, 15,000]) = 10,000, so a costlier retest raises this
        # agent's frozen cost off its opening-day 5,000 instead of being
        # diluted three-to-one by same-seed replicates.
        async with session.begin():
            session.add(
                ConfirmationScore(
                    agent_id=agents[0].agent_id,
                    validator_hotkey="5RetestValidator",
                    bench_version=9,
                    seed=8675310,
                    composite=0.8,
                    run_id="continual-cost-run",
                    signature="ab" * 64,
                    v9_efficiency_token_total=15_000,
                    v9_efficiency_cost_eligible=True,
                    created_at=now,
                )
            )

        async def finalized(_session: AsyncSession):
            return rows

        async def quorum(_session, agent_ids, *, bench_versions):
            del agent_ids, bench_versions
            return score_rows

        monkeypatch.setattr(
            "ditto.api_server.efficiency._finalized_ranked_rows", finalized
        )
        monkeypatch.setattr("ditto.db.queries.scores.quorum_score_rows", quorum)
        config = EfficiencyBonusConfig(
            enabled=True,
            min_cohort=5,
            cohort_size=25,
            factor_alpha=0.25,
            minimum_factor=0.85,
            maximum_factor=1.10,
        )

        await ensure_efficiency_state(session, config, now=now)

        snapshot = await latest_snapshot(
            session,
            bench_version=9,
            run_size="full",
            max_epoch_index=10**12,
        )
        assert snapshot is not None
        assert snapshot.curve_version == CURVE_VERSION_BOUNDED_FACTOR
        # Dynamic cohort statistic: N=5 -> nearest-rank P25 is the second cost.
        assert snapshot.reference_p25_tokens == 10_000.0
        assert snapshot.factor_alpha == 0.25
        assert snapshot.minimum_factor == 0.85
        assert snapshot.maximum_factor == 1.10
        snapshot_id = snapshot.snapshot_id
        snapshot_epoch = snapshot.epoch_index

        assignments = await get_bonus_rows(
            session,
            [agent.agent_id for agent in agents],
            bench_versions={agent.agent_id: 9 for agent in agents},
            epoch_index=snapshot_epoch,
        )
        assert len(assignments) == 5
        assert all(row.bonus == 0.0 for row in assignments.values())
        assert assignments[agents[0].agent_id].token_total == 10_000.0
        # Its retest seed carried it to the reference exactly, so it is neutral.
        assert assignments[agents[0].agent_id].factor == pytest.approx(1.0)
        assert assignments[agents[1].agent_id].token_total == 10_000.0
        assert assignments[agents[1].agent_id].factor == 1.0
        # (10,000 / 20,000) ** 0.25 = 0.8409, below the frozen 0.85 floor.
        assert assignments[agents[2].agent_id].factor == 0.85

        # A config edit inside the same epoch cannot rewrite the snapshot or
        # assignments. The P25 and knobs are facts frozen at epoch creation.
        await session.rollback()
        await ensure_efficiency_state(
            session,
            EfficiencyBonusConfig(
                enabled=True,
                min_cohort=5,
                factor_alpha=0.5,
                minimum_factor=0.9,
                maximum_factor=1.05,
            ),
            now=now,
        )
        replayed = await get_snapshot(
            session,
            bench_version=9,
            run_size="full",
            epoch_index=snapshot_epoch,
        )
        assert replayed is not None
        assert replayed.snapshot_id == snapshot_id
        assert replayed.reference_p25_tokens == 10_000.0
        assert replayed.factor_alpha == 0.25

"""Unit tests for :mod:`ditto.db.queries.scores` against SQLite-in-memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ConfirmationScore,
    EvaluationPayment,
)
from ditto.db.queries.confirmation_scores import confirmation_composites_by_seed
from ditto.db.queries.score_ranking import official_composites
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    list_eligible_ledger,
    list_provisional_ledger,
    list_scores_for_agent,
    quorum_composites,
    quorum_ledger_proof_rows,
    upsert_score,
)

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_GEN_AT = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
_FIRST_SEEN = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
# The generic "a benchmark version" fixture value. Nothing about the median,
# dedupe or ranking rules below depends on which era they run in; the value used
# to be 2 only because 2 was the oldest number available. The retired-era floor
# makes that choice load-bearing -- ``scores_bench_version_floor`` refuses
# anything under MIN_SCOREABLE_BENCH_VERSION -- so the arbitrary value is now the
# live era. Tests that need two distinct versions count up from here.
_BENCH_VERSION = 7


@pytest.fixture(autouse=True)
async def _live_era_has_activated(session: AsyncSession) -> None:
    """Put the database in the state production is in: v7 is the active era.

    A freshly migrated, empty database answers ``active_bench_version`` with
    ``DEFAULT_BENCH_VERSION`` (2), because no rollout has ever activated in it.
    That was invisible while the fixtures wrote v2 scores and it is fatal now:
    the floor means no v2 row can exist, so every default ledger read would
    resolve to an era with nothing in it and return an empty list for reasons
    that have nothing to do with what these tests assert.

    Production has never been in that state -- the v6 -> v7 transition activated
    long ago -- so plant that activated row and let the reads resolve the era the
    fleet is actually on. ``from_version`` of 6 is below the floor and stays
    writable on purpose: only ``desired_version`` is constrained, because a
    rollout records where the fleet came FROM.
    """
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_BENCH_VERSION - 1,
                desired_version=_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=_FIRST_SEEN - timedelta(days=1),
                activated_at=_FIRST_SEEN - timedelta(hours=12),
            )
        )


async def _seed_agent(session: AsyncSession) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=_MINER,
        name="alpha",
        sha256="ab" * 32,
        status=AgentStatus.EVALUATING,
        created_at=datetime.now(UTC),
    )
    async with session.begin():
        session.add(agent)
    return agent


async def _upsert(session: AsyncSession, agent_id: object, **overrides: object) -> None:
    kwargs: dict = {
        "agent_id": agent_id,
        "validator_hotkey": _VALIDATOR,
        "bench_version": _BENCH_VERSION,
        "run_id": "run_1",
        "seed": 42,
        "composite": 0.7,
        "tool_mean": 0.8,
        "memory_mean": 0.6,
        "median_ms": 500,
        "n": 20,
        "generated_at": _GEN_AT,
        "details": None,
    }
    kwargs.update(overrides)
    async with session.begin():
        await upsert_score(session, **kwargs)


class TestUpsertScore:
    async def test_inserts_new_row(self, session: AsyncSession) -> None:
        agent = await _seed_agent(session)
        await _upsert(session, agent.agent_id, details={"per_case": [{"x": 1}]})

        scores = await list_scores_for_agent(session, agent_id=agent.agent_id)
        assert len(scores) == 1
        assert scores[0].run_id == "run_1"
        assert scores[0].details == {"per_case": [{"x": 1}]}

    async def test_second_upsert_overwrites_same_row(
        self, session: AsyncSession
    ) -> None:
        agent = await _seed_agent(session)
        await _upsert(session, agent.agent_id, run_id="run_1", composite=0.4)
        await _upsert(session, agent.agent_id, run_id="run_2", composite=0.95)

        scores = await list_scores_for_agent(session, agent_id=agent.agent_id)
        assert len(scores) == 1
        assert scores[0].run_id == "run_2"
        assert scores[0].composite == pytest.approx(0.95)

    async def test_unknown_agent_raises_integrity(self, session: AsyncSession) -> None:
        with pytest.raises(DbIntegrityError):
            await _upsert(session, uuid4())

    async def test_database_rejects_a_composite_above_one(
        self, session: AsyncSession
    ) -> None:
        # ``scores_composite_range_check`` is era-independent, so this used to
        # say v5 for no reason other than that v5 was available. Written at the
        # live era it still proves the range check, and only the range check --
        # at v5 it would now be indistinguishable from the floor rejecting the
        # row before the composite was ever looked at.
        agent = await _seed_agent(session)
        with pytest.raises(DbIntegrityError):
            await _upsert(session, agent.agent_id, composite=1.001)

    async def test_list_empty_when_unscored(self, session: AsyncSession) -> None:
        agent = await _seed_agent(session)
        assert await list_scores_for_agent(session, agent_id=agent.agent_id) == []


class TestQuorumLedgerProofRows:
    async def test_projects_only_wire_evidence_from_details(
        self, session: AsyncSession
    ) -> None:
        agent = await _seed_agent(session)
        evidence = {
            "ticket_deadline": "2026-08-14T18:00:00+00:00",
            "transcript_sha256": "cd" * 32,
            "base_evidence_sha256": "ef" * 32,
            "v9_base": {"run_id": "run_1"},
        }
        await _upsert(
            session,
            agent.agent_id,
            details={**evidence, "per_case": [{"large": "payload"}]},
        )

        rows = await quorum_ledger_proof_rows(
            session,
            [agent.agent_id],
            bench_versions={agent.agent_id: _BENCH_VERSION},
        )

        assert rows[agent.agent_id][0].details == evidence

    async def test_empty_agent_set_skips_query(self, session: AsyncSession) -> None:
        assert await quorum_ledger_proof_rows(session, [], bench_versions={}) == {}


async def _seed_scored(
    session: AsyncSession,
    *,
    miner: str,
    composite: float,
    created_at: datetime,
    size_bytes: int = 524288,
    status: AgentStatus = AgentStatus.SCORED,
    n: int = 20,
    normalized_source_hash: str | None = None,
    prompt_fingerprint: dict | None = None,
    code_embedding: list | None = None,
    code_embed_model: str | None = None,
    coldkey: str | None = None,
    name: str = "agent",
) -> Agent:
    """Seed one agent + its score row, in the given lifecycle state."""
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=miner,
        name=name,
        sha256="ab" * 32,
        size_bytes=size_bytes,
        status=status,
        created_at=created_at,
        normalized_source_hash=normalized_source_hash,
        prompt_fingerprint=prompt_fingerprint,
        code_embedding=code_embedding,
        code_embed_model=code_embed_model,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        if coldkey is not None:
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{agent.agent_id.hex}",
                    extrinsic_index=0,
                    agent_id=agent.agent_id,
                    miner_hotkey=miner,
                    miner_coldkey=coldkey,
                    amount_rao=1,
                    dest_address="5Destination",
                    timestamp=created_at,
                )
            )
        await upsert_score(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=_VALIDATOR,
            bench_version=_BENCH_VERSION,
            run_id="run_1",
            seed=42,
            composite=composite,
            tool_mean=composite,
            memory_mean=composite,
            median_ms=500,
            n=n,
            generated_at=_GEN_AT,
            signature="ab" * 64,
        )
    return agent


class TestListEligibleLedger:
    async def test_empty_when_nothing_scored(self, session: AsyncSession) -> None:
        assert await list_eligible_ledger(session) == []

    async def test_internal_read_can_keep_all_owner_generations(
        self, session: AsyncSession
    ) -> None:
        coldkey = "5SharedOwner" + "x" * 36
        first = await _seed_scored(
            session,
            miner=_MINER,
            composite=0.8,
            created_at=_FIRST_SEEN,
            n=MIN_ELIGIBLE_CASES,
            coldkey=coldkey,
        )
        second = await _seed_scored(
            session,
            miner=_MINER_B,
            composite=0.8,
            created_at=_FIRST_SEEN + timedelta(minutes=1),
            n=MIN_ELIGIBLE_CASES,
            coldkey=coldkey,
        )

        public = await list_eligible_ledger(session)
        internal = await list_eligible_ledger(session, dedupe_owners=False)

        assert len(public) == 1
        assert {row.agent_id for row in internal} == {first.agent_id, second.agent_id}
        assert len({row.emission_owner_root for row in internal}) == 1

    async def test_can_omit_large_score_details(self, session: AsyncSession) -> None:
        agent = await _seed_scored(
            session,
            miner=_MINER,
            composite=0.8,
            created_at=_GEN_AT,
        )
        await _upsert(
            session,
            agent.agent_id,
            composite=0.8,
            details={"per_case": [{"large": "payload"}]},
        )

        full = await list_eligible_ledger(session)
        narrow = await list_eligible_ledger(session, include_details=False)

        assert full[0].details == {"per_case": [{"large": "payload"}]}
        assert narrow[0].details is None
        assert narrow[0].agent_id == full[0].agent_id
        assert narrow[0].composite == full[0].composite

    async def test_best_agent_per_miner_only(self, session: AsyncSession) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # Same miner, two scored agents; only the higher composite should appear.
        await _seed_scored(session, miner=_MINER, composite=0.5, created_at=t0)
        best = await _seed_scored(
            session, miner=_MINER, composite=0.8, created_at=t0.replace(hour=13)
        )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].agent_id == best.agent_id
        assert ledger[0].composite == pytest.approx(0.8)
        assert ledger[0].miner_hotkey == _MINER
        assert ledger[0].signature == "ab" * 64

    async def test_best_agent_per_coldkey_across_hotkeys_and_names(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        coldkey = "5ColdkeyOwner"
        await _seed_scored(
            session,
            miner=_MINER,
            coldkey=coldkey,
            name="mnemo-v4",
            composite=0.91,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )
        best = await _seed_scored(
            session,
            miner=_MINER_B,
            coldkey=coldkey,
            name="mnemo-v5",
            composite=0.95,
            created_at=t0.replace(hour=13),
            n=MIN_ELIGIBLE_CASES,
        )

        ledger = await list_eligible_ledger(session)

        assert len(ledger) == 1
        assert ledger[0].agent_id == best.agent_id
        assert ledger[0].miner_hotkey == _MINER_B
        assert ledger[0].miner_coldkey == coldkey

    async def test_newer_lower_score_does_not_shadow_coldkey_best(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        coldkey = "5ColdkeyOwner"
        best = await _seed_scored(
            session,
            miner=_MINER,
            coldkey=coldkey,
            composite=0.95,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session,
            miner=_MINER_B,
            coldkey=coldkey,
            composite=0.90,
            created_at=t0.replace(hour=13),
            n=MIN_ELIGIBLE_CASES,
        )

        ledger = await list_eligible_ledger(session)

        assert [row.agent_id for row in ledger] == [best.agent_id]

    async def test_official_score_can_replace_a_lucky_family_incumbent(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Family selection must use the score that ranks and pays the row.

        The incumbent won its initial quorum but fell after continual retests.
        A later generation whose canonical score is still lower should replace
        it once that lower score is higher than the incumbent's official score.
        Historical snapshots deliberately retain the canonical selection.
        """
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        coldkey = "5ColdkeyOwner"
        incumbent = await _seed_scored(
            session,
            miner=_MINER,
            coldkey=coldkey,
            composite=0.990,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )
        challenger = await _seed_scored(
            session,
            miner=_MINER_B,
            coldkey=coldkey,
            composite=0.985,
            created_at=t0.replace(hour=13),
            n=MIN_ELIGIBLE_CASES,
        )

        for agent, score in ((incumbent, 0.990), (challenger, 0.985)):
            await _upsert(
                session,
                agent.agent_id,
                validator_hotkey=_VALIDATOR + "-2",
                composite=score,
            )
            await _upsert(
                session,
                agent.agent_id,
                validator_hotkey=_VALIDATOR + "-3",
                composite=score,
            )
        async with session.begin():
            session.add(
                ConfirmationScore(
                    agent_id=incumbent.agent_id,
                    validator_hotkey=_VALIDATOR,
                    bench_version=_BENCH_VERSION,
                    seed=99,
                    composite=0.846,
                    run_id="confirmation-99",
                )
            )

        async def _continual_active(*_: object, **__: object) -> bool:
            return True

        monkeypatch.setattr(
            "ditto.db.queries.score_ranking.continual_mean_is_active",
            _continual_active,
        )

        official = await list_eligible_ledger(session)
        historical = await list_eligible_ledger(session, owner_score="canonical")

        assert [row.agent_id for row in official] == [challenger.agent_id]
        assert official[0].official_composite == pytest.approx(0.985)
        assert [row.agent_id for row in historical] == [incumbent.agent_id]

    async def test_sql_official_scores_match_python_fold(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SQL aggregate must stay byte-for-byte aligned with consensus."""
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        agent = await _seed_scored(
            session,
            miner=_MINER,
            coldkey="5ColdkeyA",
            composite=0.90,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )
        for suffix, score in (("-2", 0.80), ("-3", 1.00)):
            await _upsert(
                session,
                agent.agent_id,
                validator_hotkey=_VALIDATOR + suffix,
                composite=score,
            )
        canonical_only = await _seed_scored(
            session,
            miner=_MINER_B,
            coldkey="5ColdkeyB",
            composite=0.55,
            created_at=t0.replace(hour=13),
            n=MIN_ELIGIBLE_CASES,
        )
        for suffix, score in (("-2", 0.45), ("-3", 0.65)):
            await _upsert(
                session,
                canonical_only.agent_id,
                validator_hotkey=_VALIDATOR + "-fallback" + suffix,
                composite=score,
            )
        async with session.begin():
            session.add_all(
                [
                    ConfirmationScore(
                        agent_id=agent.agent_id,
                        validator_hotkey=f"{_VALIDATOR}-c{suffix}",
                        bench_version=_BENCH_VERSION,
                        seed=seed,
                        composite=score,
                        run_id=f"confirmation-{seed}-{suffix}",
                    )
                    for seed, values in ((100, (0.70, 0.90)), (101, (0.60, 0.80, 1.00)))
                    for suffix, score in enumerate(values, start=1)
                ]
            )

        async def _continual_active(*_: object, **__: object) -> bool:
            return True

        monkeypatch.setattr(
            "ditto.db.queries.score_ranking.continual_mean_is_active",
            _continual_active,
        )
        rows = await list_eligible_ledger(
            session, include_details=False, include_fingerprints=False
        )
        quorum = await quorum_composites(
            session,
            [row.agent_id for row in rows],
            bench_versions=dict.fromkeys(
                [row.agent_id for row in rows], _BENCH_VERSION
            ),
        )
        confirmations = await confirmation_composites_by_seed(
            session,
            agent_ids=[agent.agent_id],
            bench_version=_BENCH_VERSION,
        )
        expected = official_composites(
            rows,
            quorum=quorum,
            completed_waves=confirmations,
            continual_mean_active=True,
        )

        actual = {row.agent_id: row.official_composite for row in rows}
        assert actual == pytest.approx(expected)
        assert actual[canonical_only.agent_id] == pytest.approx(0.55)

    async def test_different_coldkeys_keep_separate_positions(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            coldkey="5ColdkeyA",
            composite=0.95,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session,
            miner=_MINER_B,
            coldkey="5ColdkeyB",
            composite=0.90,
            created_at=t0,
            n=MIN_ELIGIBLE_CASES,
        )

        assert len(await list_eligible_ledger(session)) == 2

    async def test_normalized_source_hash_flows_through(
        self, session: AsyncSession
    ) -> None:
        # The exact-repack hash must reach the ledger so the gate can read it.
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=0.7,
            created_at=t0,
            normalized_source_hash="ns" * 32,
        )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].normalized_source_hash == "ns" * 32

    async def test_prompt_fingerprint_flows_through(
        self, session: AsyncSession
    ) -> None:
        # The prompt sketch (shadow signal) must reach the ledger for the gate.
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        sketch = {"v": "p1", "k": 256, "card": 2, "m": ["aa", "bb"]}
        await _seed_scored(
            session,
            miner=_MINER,
            composite=0.7,
            created_at=t0,
            prompt_fingerprint=sketch,
        )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].prompt_fingerprint == sketch

    async def test_code_embedding_flows_through(self, session: AsyncSession) -> None:
        # The code-embedding vector + its model tag must reach the ledger for the gate
        # cosine.
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        vector = [0.1, 0.2, 0.3]
        await _seed_scored(
            session,
            miner=_MINER,
            composite=0.7,
            created_at=t0,
            code_embedding=vector,
            code_embed_model="Qwen/Qwen3-Embedding-0.6B@main",
        )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].code_embedding == vector
        assert ledger[0].code_embed_model == "Qwen/Qwen3-Embedding-0.6B@main"

    async def test_ordered_by_composite_desc(self, session: AsyncSession) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _seed_scored(session, miner=_MINER, composite=0.4, created_at=t0)
        await _seed_scored(session, miner=_MINER_B, composite=0.9, created_at=t0)
        ledger = await list_eligible_ledger(session)
        assert [e.miner_hotkey for e in ledger] == [_MINER_B, _MINER]

    async def test_eligible_flag_reflects_case_count(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _seed_scored(
            session, miner=_MINER, composite=0.5, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        await _seed_scored(session, miner=_MINER_B, composite=0.5, created_at=t0, n=12)
        by_miner = {e.miner_hotkey: e for e in await list_eligible_ledger(session)}
        assert by_miner[_MINER].eligible is True
        assert by_miner[_MINER_B].eligible is False

    async def test_eligible_ranked_above_higher_provisional(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # A full run at a *lower* composite must outrank a provisional smoke run
        # at a higher composite — the whole point of the gate.
        await _seed_scored(
            session, miner=_MINER, composite=0.55, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        await _seed_scored(session, miner=_MINER_B, composite=0.90, created_at=t0, n=12)
        ledger = await list_eligible_ledger(session)
        assert [e.miner_hotkey for e in ledger] == [_MINER, _MINER_B]
        assert ledger[0].eligible is True and ledger[1].eligible is False

    async def test_full_run_not_shadowed_by_inflated_small(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # Same miner: a real full run (lower composite) and a lucky small run
        # (higher composite). The miner must be represented by the *full* run so
        # the small one cannot both hide the full result and be dropped by the
        # emission gate.
        await _seed_scored(
            session, miner=_MINER, composite=0.52, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=0.95,
            created_at=t0.replace(hour=13),
            n=12,
        )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].eligible is True
        assert ledger[0].composite == 0.52

    async def test_zero_composite_full_run_is_not_ranked(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # A full run that scored 0.000 must NOT be ranked or crowned: the validator
        # drops composite <= 0 from the fold, so a failed full run earns nothing and
        # must never outrank a positive provisional run on the board.
        await _seed_scored(
            session, miner=_MINER, composite=0.0, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        await _seed_scored(session, miner=_MINER_B, composite=0.9, created_at=t0, n=12)
        ledger = await list_eligible_ledger(session)
        by_miner = {e.miner_hotkey: e for e in ledger}
        assert by_miner[_MINER].eligible is False  # full but zero-scoring
        assert by_miner[_MINER_B].eligible is False  # provisional smoke run
        # Neither ranks; the positive provisional sorts above the zero full run.
        assert [e.miner_hotkey for e in ledger] == [_MINER_B, _MINER]

    async def test_positive_full_run_ranked_above_zero_full_run(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # Two full runs, one positive and one zero: only the positive one is ranked.
        await _seed_scored(
            session, miner=_MINER, composite=0.0, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        await _seed_scored(
            session, miner=_MINER_B, composite=0.4, created_at=t0, n=MIN_ELIGIBLE_CASES
        )
        ledger = await list_eligible_ledger(session)
        by_miner = {e.miner_hotkey: e for e in ledger}
        assert by_miner[_MINER_B].eligible is True
        assert by_miner[_MINER].eligible is False
        assert ledger[0].miner_hotkey == _MINER_B

    async def test_excludes_non_scored_states(self, session: AsyncSession) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # Held / evaluating agents must not leak into the eligible ledger.
        await _seed_scored(
            session,
            miner=_MINER,
            composite=0.95,
            created_at=t0,
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        await _seed_scored(
            session,
            miner=_MINER_B,
            composite=0.6,
            created_at=t0,
            status=AgentStatus.SCORED,
        )
        ledger = await list_eligible_ledger(session)
        assert [e.miner_hotkey for e in ledger] == [_MINER_B]

    async def test_multiple_score_rows_returns_consistent_row(
        self, session: AsyncSession
    ) -> None:
        # An agent scored by the k=3 validator pool has three score rows. The
        # ledger must return the MEDIAN row whole (composite, seed, run_id,
        # signature, validator_hotkey all from the same physical row), not a
        # per-column aggregate that stitches a mismatched (composite, signature).
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=_MINER,
            name="agent",
            sha256="ab" * 32,
            size_bytes=524288,
            status=AgentStatus.SCORED,
            created_at=t0,
        )
        async with session.begin():
            session.add(agent)
            await session.flush()
            # Low composite from an alphabetically-later validator hotkey.
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey="5Zzz_validator",
                bench_version=_BENCH_VERSION,
                run_id="run_low",
                seed=111,
                composite=0.70,
                tool_mean=0.7,
                memory_mean=0.7,
                median_ms=500,
                n=20,
                generated_at=_GEN_AT,
                signature="11" * 64,
            )
            # High composite from an alphabetically-earlier validator hotkey.
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey="5Aaa_validator",
                bench_version=_BENCH_VERSION,
                run_id="run_high",
                seed=222,
                composite=0.90,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=500,
                n=20,
                generated_at=_GEN_AT,
                signature="99" * 64,
            )
            # Median composite from a third validator: this is the row the
            # ledger must surface whole under median-of-3.
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey="5Mmm_validator",
                bench_version=_BENCH_VERSION,
                run_id="run_mid",
                seed=333,
                composite=0.80,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=500,
                n=20,
                generated_at=_GEN_AT,
                signature="55" * 64,
            )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        e = ledger[0]
        # Every field must come from the MEDIAN row (composite 0.80), whole.
        assert e.composite == pytest.approx(0.80)
        assert e.seed == 333
        assert e.run_id == "run_mid"
        assert e.signature == "55" * 64
        assert e.validator_hotkey == "5Mmm_validator"

    async def test_median_of_three_ignores_outlier_validator(
        self, session: AsyncSession
    ) -> None:
        # Three validators score the same agent. The canonical score is the
        # MEDIAN (0.55), so a single generous (0.99) or harsh (0.10) validator
        # cannot move it — and it is the median, not the mean (~0.547).
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        agent = Agent(
            agent_id=uuid4(),
            miner_hotkey=_MINER,
            name="agent",
            sha256="cd" * 32,
            status=AgentStatus.SCORED,
            created_at=t0,
        )
        async with session.begin():
            session.add(agent)
            await session.flush()
            for vh, comp in (("5A_v", 0.10), ("5B_v", 0.55), ("5C_v", 0.99)):
                await upsert_score(
                    session,
                    agent_id=agent.agent_id,
                    validator_hotkey=vh,
                    bench_version=_BENCH_VERSION,
                    run_id=f"run_{vh}",
                    seed=1,
                    composite=comp,
                    tool_mean=comp,
                    memory_mean=comp,
                    median_ms=1,
                    n=MIN_ELIGIBLE_CASES,
                    generated_at=_GEN_AT,
                )
        ledger = await list_eligible_ledger(session)
        assert len(ledger) == 1
        assert ledger[0].composite == pytest.approx(0.55)
        assert ledger[0].eligible is True


# Band arithmetic for the era these tests run in (_BENCH_VERSION >= 6, so the
# high-score decay applies): at a 0.90 winner the fold's flat indifference band
# is 0.007 * exp(-2 * (0.90 - 0.60)) ~= 0.00384 composite points. The two
# ancestor scores below sit either side of that by a wide margin, so nothing here
# turns on the third decimal place of the decay.
_WINNER_COMPOSITE = 0.90
_WITHIN_BAND = 0.8999
# Two benchmark steps behind the winner: inside the *dethrone* band and outside
# the crown-anchor band. This is the shape that crowned white-bolt on
# 2026-08-13 while it had never once led the rival it outranked, and it is the
# whole reason the two bands are separate numbers.
#
# Was 0.8993 -- 0.0007 back -- until the anchor floor was held to
# MIN_RESOLVABLE_COMPOSITE_STEP. That original gap is *below* what bench v9 can
# express (0.25/251 = 0.000996 per step), so no real ancestor can sit there:
# the case it described was untestable in production and the distinction it
# asserted was one the benchmark cannot make. At two steps the ancestor is
# measurably behind, which is what "never led" has to mean to be enforceable.
_BEHIND_A_RIVAL = 0.898
# One real bench-v9 step behind the winner: 251 memory cases in half-point
# steps, halved by the tool mean, is 0.25/251 = 0.000996 of composite. Spelled
# as a literal rather than derived from MIN_RESOLVABLE_COMPOSITE_STEP so the
# test asserts against the benchmark's actual resolution and keeps failing if
# that constant is ever tuned below it.
_ONE_STEP_BACK = 0.90 - 0.000996
_OUTSIDE_BAND = 0.88


class TestCrownFirstSeen:
    """The KOTH fold anchors on the lineage's arrival, not the winning upload.

    The fold makes the earliest ``first_seen`` the provisional champion and asks
    everyone later to clear an indifference band, so this timestamp decides who
    holds a tie. Reading it off the winning submission meant a miner tied at the
    top handed the crown to its rival by resubmitting: the owner's representative
    row moved to the new agent, the anchor moved forward with it, and the rival's
    older row became the incumbent that the improved agent could no longer beat.
    """

    async def test_lineage_keeps_its_anchor_across_a_resubmission(
        self, session: AsyncSession
    ) -> None:
        """The defect, stated directly: improving must not forfeit seniority."""
        first = datetime(2026, 6, 8, 15, 52, tzinfo=UTC)
        later = datetime(2026, 6, 8, 21, 20, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WITHIN_BAND,
            created_at=first,
            n=MIN_ELIGIBLE_CASES,
            name="v47",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=later,
            n=MIN_ELIGIBLE_CASES,
            name="v49",
        )

        (row,) = await list_eligible_ledger(session)

        assert row.crown_first_seen == first
        assert row.fold_first_seen == first
        # The two questions stay separate: this tarball really did arrive later,
        # and the anti-copy comparison and public board still say so.
        assert row.first_seen == later

    async def test_an_ancestor_that_never_led_confers_no_seniority(
        self, session: AsyncSession
    ) -> None:
        """The 2026-08-13 crown, in miniature.

        An owner arrives early but *behind*, a rival passes it and holds the top
        score for days, then the owner matches that score with a new generation
        and takes the crown on the early arrival. Sharing one band between
        seniority and indifference made that inheritance legal: the ancestor was
        inside the dethrone band, so it read as "already at this score" despite
        never having led anyone. The crown-anchor band is tighter precisely so
        this ancestor confers nothing and the winner stands on its own arrival.
        """
        early_but_behind = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        matched_it_later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_BEHIND_A_RIVAL,
            created_at=early_but_behind,
            n=MIN_ELIGIBLE_CASES,
            name="early-but-behind",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=matched_it_later,
            n=MIN_ELIGIBLE_CASES,
            name="matched-it-later",
        )

        (row,) = await list_eligible_ledger(session)

        assert row.crown_first_seen == matched_it_later
        assert row.fold_first_seen == matched_it_later

    async def test_a_generation_the_owner_regressed_from_confers_no_seniority(
        self, session: AsyncSession
    ) -> None:
        """The other direction, and the one a tighter band could never reach.

        The owner peaks early, then its next generation scores *below* that
        peak. Measuring the band from the family's best made the peak admit
        itself unconditionally, so it kept handing its arrival time to a
        generation defending a score the lineage had already lost — on
        2026-08-14 a 0.995020 submission held the crown that way. Measuring from
        the row's own score is what makes the peak stop counting: the owner is
        no longer at it.
        """
        peaked_early = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        fell_back_later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=peaked_early,
            n=MIN_ELIGIBLE_CASES,
            name="peaked-early",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_BEHIND_A_RIVAL,
            created_at=fell_back_later,
            n=MIN_ELIGIBLE_CASES,
            name="fell-back-later",
        )

        rows = await list_eligible_ledger(session, dedupe_owners=False)
        by_arrival = {row.first_seen: row for row in rows}

        # The regressed generation stands on its own arrival ...
        assert by_arrival[fell_back_later].crown_first_seen == fell_back_later
        assert by_arrival[fell_back_later].fold_first_seen == fell_back_later
        # ... while the peak keeps the reign it actually earned.
        assert by_arrival[peaked_early].crown_first_seen == peaked_early

    async def test_improving_by_one_benchmark_step_keeps_the_anchor(
        self, session: AsyncSession
    ) -> None:
        """Getting better must never cost an owner its seniority.

        The band's floor is held to :data:`MIN_RESOLVABLE_COMPOSITE_STEP` for
        this case alone. A generation one step back is not distinguishable from
        the current one by anything the benchmark can measure, so treating it as
        a different score would charge the owner for the smallest improvement it
        is possible to make -- and a one-step gain is nowhere near enough to
        dethrone anyone, so the owner would pay the seniority and get nothing.
        On 2026-08-14 five of the six rows that stood to lose an inherited
        anchor were in exactly this position.
        """
        improved_from = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        improved_at = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_ONE_STEP_BACK,
            created_at=improved_from,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=improved_at,
            n=MIN_ELIGIBLE_CASES,
        )

        (row,) = await list_eligible_ledger(session)

        assert row.composite == pytest.approx(_WINNER_COMPOSITE)
        assert row.crown_first_seen == improved_from
        assert row.fold_first_seen == improved_from

    async def test_a_tied_resubmission_represents_the_owner_and_keeps_its_anchor(
        self, session: AsyncSession
    ) -> None:
        """Both halves of the miner-facing promise, in one place.

        Bench v9 saturated, so a miner's real improvements land on the *same*
        composite and the tie decides who is on the board. Earliest-wins left
        the owner represented by a generation it had already moved past, with no
        way to be seen — while the seniority that tie-break was protecting is
        not in the tie at all, it is in ``crown_first_seen``.
        """
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        resubmitted = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        for created_at in (first, resubmitted):
            await _seed_scored(
                session,
                miner=_MINER,
                composite=_WINNER_COMPOSITE,
                created_at=created_at,
                n=MIN_ELIGIBLE_CASES,
            )

        (row,) = await list_eligible_ledger(session)

        # The newer generation is the one on the board ...
        assert row.first_seen == resubmitted
        # ... standing on the reign the lineage already earned.
        assert row.crown_first_seen == first
        assert row.fold_first_seen == first

    async def test_a_worse_resubmission_never_takes_the_owners_place(
        self, session: AsyncSession
    ) -> None:
        """Newest wins a tie, not an argument. Experimenting stays free."""
        strong = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        weak_and_newer = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=strong,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_BEHIND_A_RIVAL,
            created_at=weak_and_newer,
            n=MIN_ELIGIBLE_CASES,
        )

        (row,) = await list_eligible_ledger(session)

        assert row.first_seen == strong
        assert row.composite == pytest.approx(_WINNER_COMPOSITE)

    async def test_a_plateau_resubmission_keeps_its_anchor(
        self, session: AsyncSession
    ) -> None:
        """The invariant the band exists to protect, in its own test.

        Saturated agents re-measure to the same composite, so a miner iterating
        at a plateau must not forfeit seniority — that is the entire reason the
        anchor is a lineage value. Both generations resolve to the first
        arrival, in either direction of the band.
        """
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        resubmitted = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        for name, created_at in (("v1", first), ("v2", resubmitted)):
            await _seed_scored(
                session,
                miner=_MINER,
                composite=_WINNER_COMPOSITE,
                created_at=created_at,
                n=MIN_ELIGIBLE_CASES,
                name=name,
            )

        rows = await list_eligible_ledger(session, dedupe_owners=False)

        assert {row.crown_first_seen for row in rows} == {first}

    async def test_a_distinctly_worse_ancestor_confers_no_seniority(
        self, session: AsyncSession
    ) -> None:
        """Seniority is earned at a score, not by having shown up early."""
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_OUTSIDE_BAND,
            created_at=first,
            n=MIN_ELIGIBLE_CASES,
            name="early-but-weak",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=later,
            n=MIN_ELIGIBLE_CASES,
            name="winner",
        )

        (row,) = await list_eligible_ledger(session)

        assert row.crown_first_seen == later

    async def test_a_banned_ancestor_confers_no_seniority(
        self, session: AsyncSession
    ) -> None:
        """A generation removed from the pool cannot backdate its replacement."""
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WITHIN_BAND,
            created_at=first,
            n=MIN_ELIGIBLE_CASES,
            status=AgentStatus.BANNED,
            name="banned-ancestor",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=later,
            n=MIN_ELIGIBLE_CASES,
            name="winner",
        )

        (row,) = await list_eligible_ledger(session)

        assert row.crown_first_seen == later

    async def test_an_unranked_ancestor_confers_no_seniority(
        self, session: AsyncSession
    ) -> None:
        """A smoke/practice run is trivially aced; its date must not count."""
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=first,
            n=MIN_ELIGIBLE_CASES // 2,
            name="smoke-run",
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=later,
            n=MIN_ELIGIBLE_CASES,
            name="winner",
        )

        (row,) = await list_eligible_ledger(session)

        assert row.eligible is True
        assert row.crown_first_seen == later

    async def test_target_only_agent_waits_for_frozen_rollout_completion(
        self, session: AsyncSession
    ) -> None:
        """Desired-only scores remain progress until the frozen cohort finishes."""
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        later = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _open_rollout(session)
        ancestor = await _seed_scored(
            session,
            miner=_MINER,
            composite=_WITHIN_BAND,
            created_at=first,
            n=MIN_ELIGIBLE_CASES,
            name="source-era-ancestor",
        )
        winner = Agent(
            agent_id=uuid4(),
            miner_hotkey=_MINER,
            name="target-era-winner",
            sha256="ab" * 32,
            size_bytes=524288,
            status=AgentStatus.SCORED,
            created_at=later,
        )
        async with session.begin():
            session.add(winner)
            await session.flush()
            await upsert_score(
                session,
                agent_id=winner.agent_id,
                validator_hotkey=_VALIDATOR,
                bench_version=_ROLLOUT_DESIRED,
                run_id="run_1",
                seed=42,
                composite=_WINNER_COMPOSITE,
                tool_mean=_WINNER_COMPOSITE,
                memory_mean=_WINNER_COMPOSITE,
                median_ms=500,
                n=MIN_ELIGIBLE_CASES,
                generated_at=_GEN_AT,
            )

        (row,) = await list_eligible_ledger(session)

        assert row.agent_id == ancestor.agent_id
        assert row.bench_version == _BENCH_VERSION
        assert row.crown_first_seen == first

    async def test_a_lone_submission_anchors_on_itself(
        self, session: AsyncSession
    ) -> None:
        """No family, no backdating: the anchor can only ever move earlier."""
        created = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=created,
            n=MIN_ELIGIBLE_CASES,
        )

        (row,) = await list_eligible_ledger(session)

        assert row.crown_first_seen == created
        assert row.first_seen == created

    async def test_another_owners_earlier_submission_confers_nothing(
        self, session: AsyncSession
    ) -> None:
        """The anchor is per lineage. A rival's history is not yours to inherit."""
        rival_time = datetime(2026, 6, 8, 8, 0, tzinfo=UTC)
        own_time = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        await _seed_scored(
            session,
            miner=_MINER_B,
            composite=_WINNER_COMPOSITE,
            created_at=rival_time,
            n=MIN_ELIGIBLE_CASES,
        )
        await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=own_time,
            n=MIN_ELIGIBLE_CASES,
        )

        by_hotkey = {
            row.miner_hotkey: row for row in await list_eligible_ledger(session)
        }

        assert by_hotkey[_MINER].crown_first_seen == own_time
        assert by_hotkey[_MINER_B].crown_first_seen == rival_time

    async def test_the_ledger_ranks_owners_by_crown_not_the_winning_upload(
        self, session: AsyncSession
    ) -> None:
        """Iterating at a plateau must not hand a later rival the earlier clock.

        The representative tarball is the newest tied generation, but the
        between-owner order reads the lineage arrival. A miner who resubmits
        the same score after a rival arrives still ranks ahead of that rival.
        """
        first = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
        rival = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        resubmitted = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
        for created_at in (first, resubmitted):
            await _seed_scored(
                session,
                miner=_MINER,
                composite=_WINNER_COMPOSITE,
                created_at=created_at,
                n=MIN_ELIGIBLE_CASES,
            )
        await _seed_scored(
            session,
            miner=_MINER_B,
            composite=_WINNER_COMPOSITE,
            created_at=rival,
            n=MIN_ELIGIBLE_CASES,
        )

        rows = await list_eligible_ledger(session)
        by_hotkey = {row.miner_hotkey: row for row in rows}

        assert [row.miner_hotkey for row in rows] == [_MINER, _MINER_B]
        assert by_hotkey[_MINER].first_seen == resubmitted
        assert by_hotkey[_MINER].crown_first_seen == first
        assert by_hotkey[_MINER].fold_first_seen == first
        assert by_hotkey[_MINER_B].first_seen == rival

    async def test_provisional_rows_fall_back_to_their_own_upload_time(
        self, session: AsyncSession
    ) -> None:
        """``crown_first_seen`` is an owner-family fact; rows built without one
        must behave exactly as they did before the anchor existed."""
        created = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        agent = await _seed_scored(
            session,
            miner=_MINER,
            composite=_WINNER_COMPOSITE,
            created_at=created,
            n=MIN_ELIGIBLE_CASES,
            status=AgentStatus.EVALUATING,
        )

        ((row, _rank),) = await list_provisional_ledger(session)

        assert row.agent_id == agent.agent_id
        assert row.crown_first_seen is None
        assert row.fold_first_seen == created


class TestListProvisionalLedger:
    async def test_returns_one_row_per_coldkey_not_per_hotkey(
        self, session: AsyncSession
    ) -> None:
        """The provisional overlay must group the way the emission ledger does.

        :func:`list_eligible_ledger` partitions on ``emission_owner``, so one
        coldkey funding two hotkeys holds one board position. Grouping the
        provisional feed by hotkey previewed a second position it never gets.
        """
        best = await _seed_scored(
            session,
            miner=_MINER,
            composite=0.90,
            created_at=_FIRST_SEEN,
            status=AgentStatus.EVALUATING,
            n=MIN_ELIGIBLE_CASES,
            coldkey="5SharedColdkey",
        )
        await _seed_scored(
            session,
            miner=_MINER_B,
            composite=0.89,
            created_at=_FIRST_SEEN + timedelta(minutes=1),
            status=AgentStatus.EVALUATING,
            n=MIN_ELIGIBLE_CASES,
            coldkey="5SharedColdkey",
        )

        rows = await list_provisional_ledger(session)

        assert [(row.agent_id, row.miner_coldkey) for row, _count in rows] == [
            (best.agent_id, "5SharedColdkey")
        ]

    async def test_unpaid_legacy_rows_stay_separate_owners(
        self, session: AsyncSession
    ) -> None:
        """The hotkey fallback must not collapse every payment-less row into one."""
        first = await _seed_scored(
            session,
            miner=_MINER,
            composite=0.90,
            created_at=_FIRST_SEEN,
            status=AgentStatus.EVALUATING,
            n=MIN_ELIGIBLE_CASES,
        )
        second = await _seed_scored(
            session,
            miner=_MINER_B,
            composite=0.89,
            created_at=_FIRST_SEEN + timedelta(minutes=1),
            status=AgentStatus.EVALUATING,
            n=MIN_ELIGIBLE_CASES,
        )

        rows = await list_provisional_ledger(session)

        assert {row.agent_id for row, _count in rows} == {
            first.agent_id,
            second.agent_id,
        }


class TestListScoresForBenchVersion:
    async def test_filters_by_first_class_version_not_advisory_details(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.scores import list_scores_for_bench_version

        agent = await _seed_agent(session)
        # The first-class column wins even when other rows' advisory details say
        # the next version, or say nothing. The pair used to be v1 against v2;
        # both are beneath the floor now, so the same two-era shape is expressed
        # one era up.
        await _upsert(
            session,
            agent.agent_id,
            validator_hotkey="5V1",
            run_id="r1",
            details={
                "bench_version": _BENCH_VERSION,
                "per_case": [{"expected": ["x"]}],
            },
        )
        await _upsert(
            session,
            agent.agent_id,
            validator_hotkey="5V2",
            run_id="r2",
            bench_version=_BENCH_VERSION + 1,
            details={"bench_version": _BENCH_VERSION + 1},
        )
        await _upsert(
            session,
            agent.agent_id,
            validator_hotkey="5V3",
            run_id="r3",
            bench_version=_BENCH_VERSION + 1,
            details=None,
        )

        rows, total = await list_scores_for_bench_version(
            session, version=_BENCH_VERSION
        )
        assert total == 1
        assert len(rows) == 1
        score, miner = rows[0]
        assert score.run_id == "r1"
        assert miner == _MINER
        # The unredacted answer key rides along for the (retired) corpus.
        assert score.details is not None
        assert score.details["per_case"][0]["expected"] == ["x"]


# The transition under test. It used to be v2 -> v4, chosen as two arbitrary
# adjacent-ish numbers. Both legs have to clear the floor now: the source leg
# because these tests write a full quorum of ``from_version`` scores, and the
# target leg because ``benchmark_rollout_desired_floor`` refuses a rollout that
# aims below v7. So the same shape sits one era up, on the live version and the
# one after it.
_ROLLOUT_FROM = _BENCH_VERSION
_ROLLOUT_DESIRED = _BENCH_VERSION + 1
_QUORUM_VALIDATORS = ("5Va", "5Vb", "5Vc")


async def _open_rollout(session: AsyncSession) -> None:
    """A collecting v7 -> v8 rollout, the state the threshold rule governs."""
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_ROLLOUT_FROM,
                desired_version=_ROLLOUT_DESIRED,
                status="collecting",
                cohort_size=5,
                created_at=datetime.now(UTC),
            )
        )


async def _seed_versioned_agent(
    session: AsyncSession,
    *,
    miner: str,
    created_at: datetime,
    source_composite: float,
    desired_composite: float | None = None,
    desired_samples: int = 3,
    desired_n: int = MIN_ELIGIBLE_CASES,
    status: AgentStatus = AgentStatus.SCORED,
    coldkey: str | None = None,
) -> Agent:
    """One agent with a full source-era quorum and an optional target-era one."""
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=miner,
        name="agent",
        sha256="ab" * 32,
        size_bytes=524288,
        status=status,
        created_at=created_at,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        rollout = await session.scalar(
            select(BenchmarkRollout).where(BenchmarkRollout.status == "collecting")
        )
        if rollout is not None:
            member_count = int(
                await session.scalar(
                    select(func.count(BenchmarkRolloutMember.agent_id)).where(
                        BenchmarkRolloutMember.rollout_id == rollout.rollout_id
                    )
                )
                or 0
            )
            if member_count < rollout.cohort_size:
                session.add(
                    BenchmarkRolloutMember(
                        rollout_id=rollout.rollout_id,
                        agent_id=agent.agent_id,
                        position=member_count + 1,
                        frozen_miner_hotkey=agent.miner_hotkey,
                        frozen_composite=source_composite,
                    )
                )
        if coldkey is not None:
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{agent.agent_id.hex}",
                    extrinsic_index=0,
                    agent_id=agent.agent_id,
                    miner_hotkey=miner,
                    miner_coldkey=coldkey,
                    amount_rao=1,
                    dest_address="5Destination",
                    timestamp=created_at,
                )
            )
        for index, validator in enumerate(_QUORUM_VALIDATORS):
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey=validator,
                bench_version=_ROLLOUT_FROM,
                run_id=f"source-{miner}-{index}",
                seed=index,
                composite=source_composite,
                tool_mean=source_composite,
                memory_mean=source_composite,
                median_ms=500,
                n=MIN_ELIGIBLE_CASES,
                generated_at=_GEN_AT,
                signature="ab" * 64,
            )
        if desired_composite is not None:
            for index, validator in enumerate(_QUORUM_VALIDATORS[:desired_samples]):
                await upsert_score(
                    session,
                    agent_id=agent.agent_id,
                    validator_hotkey=validator,
                    bench_version=_ROLLOUT_DESIRED,
                    run_id=f"desired-{miner}-{index}",
                    seed=index,
                    composite=desired_composite,
                    tool_mean=desired_composite,
                    memory_mean=desired_composite,
                    median_ms=500,
                    n=desired_n,
                    generated_at=_GEN_AT,
                    signature="cd" * 64,
                )
    return agent


def _miner(index: int) -> str:
    return f"5Miner{index:048d}"


class TestThresholdGatedAuthority:
    """Ledger authority flips to the desired version only past the threshold.

    The whole ledger is on ONE version at a time; the flip point is
    ``MIN_DESIRED_AUTHORITY_AGENTS`` (the KOTH champion + tail), so the emission
    set never loses recipients mid-rollout.
    """

    def test_min_desired_authority_matches_koth_recipients(self) -> None:
        # Drift guard: the threshold IS the KOTH recipient count. The constant is
        # spelled out in benchmark_rollout (importing api_server there is a
        # cycle), so this asserts the derivation it documents.
        from ditto.api_server.koth import KOTH_TAIL_SIZE
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        assert MIN_DESIRED_AUTHORITY_AGENTS == 1 + KOTH_TAIL_SIZE

    @staticmethod
    def _assert_single_version(ledger: list, expected: int) -> None:
        versions = {row.bench_version for row in ledger}
        assert versions == {expected}, f"mixed authority versions: {versions}"

    async def test_below_threshold_keeps_active_version_for_every_agent(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        # Five agents; only four hold a full v8 quorum — one short of the flip.
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50 + index / 100,
                desired_composite=(
                    0.80 if index < MIN_DESIRED_AUTHORITY_AGENTS - 1 else None
                ),
            )
        ledger = await list_eligible_ledger(session)
        # Every agent still ranked on v7, and the emission set is still full.
        self._assert_single_version(ledger, _ROLLOUT_FROM)
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS
        assert all(row.eligible for row in ledger)
        assert sorted(row.composite for row in ledger) == pytest.approx(
            [0.50 + index / 100 for index in range(MIN_DESIRED_AUTHORITY_AGENTS)]
        )

    async def test_at_threshold_flips_whole_ledger_to_desired_version(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50 + index / 100,
                desired_composite=0.70 + index / 100,
            )
        ledger = await list_eligible_ledger(session)
        self._assert_single_version(ledger, _ROLLOUT_DESIRED)
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS
        assert all(row.eligible for row in ledger)
        assert ledger[0].composite == pytest.approx(
            0.70 + (MIN_DESIRED_AUTHORITY_AGENTS - 1) / 100
        )

    async def test_duplicate_coldkey_cannot_tip_rollout_threshold(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                coldkey=("5SharedColdkey" if index < 2 else f"5Coldkey{index:048d}"),
                created_at=t0,
                source_composite=0.50 + index / 100,
                desired_composite=0.70 + index / 100,
            )

        ledger = await list_eligible_ledger(session)

        self._assert_single_version(ledger, _ROLLOUT_FROM)
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS - 1

    async def test_zero_composite_desired_quorum_ends_the_carry_forward(
        self, session: AsyncSession
    ) -> None:
        """A proven zero-inference agent must lose its old-version ceiling.

        This is the ledger half of the ``model_inference_required`` fix. While
        the scorer terminated a v10/v11 zero-inference run instead of scoring
        it, the agent had no desired-version quorum at all, so it kept being
        ranked on its saturated source-version composite indefinitely -- a
        template-fitter holding a near-perfect score the newer benchmark was
        never allowed to measure.

        The repair is that the run now finalizes at 0.00, and the invariant that
        makes it work is here: version authority is selected on quorum SIZE, not
        on composite. Adding an eligibility or ``composite > 0`` filter to
        ``desired_at_quorum`` would silently restore the carry-forward, because
        a zero row would stop counting as a desired-version quorum and the
        source-version row would win again.
        """
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.70,
            )
        ceiling = 0.997012
        zero_inference = await _seed_versioned_agent(
            session,
            miner=_miner(99),
            created_at=t0,
            source_composite=ceiling,
            desired_composite=0.0,
        )

        ledger = await list_eligible_ledger(session)

        self._assert_single_version(ledger, _ROLLOUT_DESIRED)
        assert ceiling not in {row.composite for row in ledger}
        carried = [row for row in ledger if row.agent_id == zero_inference.agent_id]
        # Either shape is correct; what must never happen is the saturated
        # source-version composite surviving the flip.
        assert not carried or (
            carried[0].composite == pytest.approx(0.0) and not carried[0].eligible
        )

    async def test_agent_without_desired_quorum_drops_out_after_flip(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.70,
            )
        laggard = await _seed_versioned_agent(
            session,
            miner=_miner(99),
            created_at=t0,
            source_composite=0.99,
            desired_composite=None,
        )
        ledger = await list_eligible_ledger(session)
        # Intended: a v7-only agent has no authoritative row once the pool is on
        # v8, however high its v7 composite was. That is why the threshold waits
        # for a full emission set first.
        self._assert_single_version(ledger, _ROLLOUT_DESIRED)
        assert laggard.agent_id not in {row.agent_id for row in ledger}
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS

    @pytest.mark.parametrize("samples", [1, 2])
    async def test_partial_desired_samples_never_count_toward_threshold(
        self, session: AsyncSession, samples: int
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        # Every agent has an incomplete v8 sample: 1/3 or 2/3 is not a quorum.
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS + 2):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.90,
                desired_samples=samples,
            )
        ledger = await list_eligible_ledger(session)
        self._assert_single_version(ledger, _ROLLOUT_FROM)
        assert all(row.composite == pytest.approx(0.50) for row in ledger)

    async def test_non_member_ranked_family_can_tip_desired_authority(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS - 1):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.70,
            )
        unranked_member = await _seed_versioned_agent(
            session,
            miner=_miner(4),
            created_at=t0,
            source_composite=0.50,
            desired_composite=0.0,
        )
        outsider = await _seed_versioned_agent(
            session,
            miner=_miner(80),
            created_at=t0,
            source_composite=0.40,
            desired_composite=0.88,
        )
        ledger = await list_eligible_ledger(session)
        self._assert_single_version(ledger, _ROLLOUT_DESIRED)
        assert outsider.agent_id in {row.agent_id for row in ledger}
        assert unranked_member.agent_id not in {
            row.agent_id for row in ledger if row.eligible
        }

    async def test_ineligible_desired_run_does_not_count_toward_threshold(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS - 1):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.70,
            )
        # A full 3/3 v8 quorum on a SMOKE run (below the full-benchmark floor).
        # It is unranked, so it must not be the agent that tips the ledger over.
        await _seed_versioned_agent(
            session,
            miner=_miner(50),
            created_at=t0,
            source_composite=0.50,
            desired_composite=0.95,
            desired_n=MIN_ELIGIBLE_CASES - 1,
        )
        # Same for a held agent: it is outside the eligible pool entirely.
        await _seed_versioned_agent(
            session,
            miner=_miner(51),
            created_at=t0,
            source_composite=0.50,
            desired_composite=0.95,
            status=AgentStatus.ATH_PENDING_REVIEW,
        )
        ledger = await list_eligible_ledger(session)
        self._assert_single_version(ledger, _ROLLOUT_FROM)
        assert len(ledger) == MIN_DESIRED_AUTHORITY_AGENTS

    async def test_explicit_bench_version_request_is_unaffected(
        self, session: AsyncSession
    ) -> None:
        from ditto.db.queries.benchmark_rollout import MIN_DESIRED_AUTHORITY_AGENTS

        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _open_rollout(session)
        # Below the threshold, so the default read is pinned to v7 — but an
        # explicit historical view must still return exactly what it asked for.
        for index in range(MIN_DESIRED_AUTHORITY_AGENTS - 1):
            await _seed_versioned_agent(
                session,
                miner=_miner(index),
                created_at=t0,
                source_composite=0.50,
                desired_composite=0.70,
            )
        default_ledger = await list_eligible_ledger(session)
        self._assert_single_version(default_ledger, _ROLLOUT_FROM)

        desired_view = await list_eligible_ledger(
            session, bench_version=_ROLLOUT_DESIRED
        )
        self._assert_single_version(desired_view, _ROLLOUT_DESIRED)
        assert len(desired_view) == MIN_DESIRED_AUTHORITY_AGENTS - 1
        assert all(row.composite == pytest.approx(0.70) for row in desired_view)

        active_view = await list_eligible_ledger(session, bench_version=_ROLLOUT_FROM)
        self._assert_single_version(active_view, _ROLLOUT_FROM)

    async def test_no_open_rollout_keeps_plain_highest_version_behaviour(
        self, session: AsyncSession
    ) -> None:
        t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        # No OPEN rollout: ``_open_rollout`` is deliberately not called, so
        # desired_version is None and nothing changes. The activated v6 -> v7 row
        # the module fixture plants is terminal and does not make a transition
        # open -- it only says which era the fleet is on.
        await _seed_versioned_agent(
            session, miner=_miner(0), created_at=t0, source_composite=0.50
        )
        ledger = await list_eligible_ledger(session)
        self._assert_single_version(ledger, _ROLLOUT_FROM)
        assert ledger[0].composite == pytest.approx(0.50)


class TestQuorumComposites:
    async def test_returns_every_composite_per_agent(
        self, session: AsyncSession
    ) -> None:
        # Three validators score one agent on three different seeds; the query
        # returns all three composites so the ledger can compute their SEM.
        agent = await _seed_agent(session)
        for vh, comp in (("5V1", 0.80), ("5V2", 0.85), ("5V3", 0.90)):
            await _upsert(
                session, agent.agent_id, validator_hotkey=vh, run_id=vh, composite=comp
            )
        out = await quorum_composites(session, [agent.agent_id])
        assert sorted(out[agent.agent_id]) == pytest.approx([0.80, 0.85, 0.90])

    async def test_empty_ids_returns_empty(self, session: AsyncSession) -> None:
        assert await quorum_composites(session, []) == {}

    async def test_unknown_agent_absent(self, session: AsyncSession) -> None:
        assert await quorum_composites(session, [uuid4()]) == {}

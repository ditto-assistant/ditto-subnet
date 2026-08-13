"""Unit tests for the append-only top-5 confirmation-score ledger queries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import Agent, ConfirmationScore
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
    completed_confirmation_wave_seeds,
    confirmation_catchup_seeds,
    confirmation_composites_by_seed,
    confirmation_depths,
    confirmation_efficiency_costs_by_agent,
    confirmation_history_by_agent,
    fold_eligible_seeds_by_agent,
    lane_seed_universe,
)

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
# The era these rows belong to. This ledger is version-scoped but none of the
# idempotence, median or seed-universe rules below care which version they run
# at; the literal used to be 2 because it was the oldest one there was.
# ``confirmation_scores_bench_version_floor`` refuses anything under
# MIN_SCOREABLE_BENCH_VERSION, so it is the live era, and the tests that need a
# second version to prove version scoping use the one after it.
_BENCH_VERSION = 7
_NEXT_BENCH_VERSION = 8


async def _seed_agent(session: AsyncSession, name: str = "a") -> UUID:
    aid = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=aid,
                miner_hotkey="5Miner",
                name=name,
                sha256="ab" * 32,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW,
            )
        )
    return aid


def _row(
    agent_id: UUID,
    validator: str,
    seed: int,
    composite: float,
    *,
    token_total: int | None = None,
    cost_eligible: bool | None = None,
) -> ConfirmationSeedScore:
    return ConfirmationSeedScore(
        agent_id=agent_id,
        validator_hotkey=validator,
        seed=seed,
        composite=composite,
        run_id=f"run-{validator}-{seed}",
        signature="ab" * 64,
        v9_efficiency_token_total=token_total,
        v9_efficiency_cost_eligible=cost_eligible,
    )


class TestAppendConfirmationScores:
    async def test_append_is_insert_idempotent_on_the_unique_key(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_agent(session)
        async with session.begin():
            n = await append_confirmation_scores(
                session,
                rows=[_row(aid, "5V1", 100, 0.80), _row(aid, "5V1", 200, 0.82)],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
        assert n == 2
        # Re-submitting the whole union (incumbent resends every round) is a no-op
        # on the already-present seeds; only the genuinely new seed is inserted.
        async with session.begin():
            n2 = await append_confirmation_scores(
                session,
                rows=[
                    _row(aid, "5V1", 100, 0.99),  # same key -> ignored (immutable)
                    _row(aid, "5V1", 200, 0.99),  # same key -> ignored
                    _row(aid, "5V1", 300, 0.85),  # new seed -> inserted
                ],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
        assert n2 == 1
        async with session.begin():
            total = await session.scalar(
                select(func.count()).select_from(ConfirmationScore)
            )
            first = await session.get(
                ConfirmationScore, (aid, _BENCH_VERSION, "5V1", 100)
            )
        assert total == 3
        # The first-written composite wins; a later resend never overwrites it.
        assert first is not None and first.composite == 0.80

    async def test_distinct_validators_and_versions_coexist(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_agent(session)
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[_row(aid, "5V1", 100, 0.80), _row(aid, "5V2", 100, 0.82)],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
            await append_confirmation_scores(
                session,
                rows=[_row(aid, "5V1", 100, 0.90)],
                bench_version=_NEXT_BENCH_VERSION,
                created_at=_NOW,
            )
        async with session.begin():
            total = await session.scalar(
                select(func.count()).select_from(ConfirmationScore)
            )
        assert total == 3


class TestConfirmationAggregates:
    async def test_efficiency_costs_include_valid_v9_retests_and_fail_closed(
        self, session: AsyncSession
    ) -> None:
        valid = await _seed_agent(session, "valid-cost")
        invalid = await _seed_agent(session, "invalid-cost")
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    _row(
                        valid,
                        "5V1",
                        100,
                        0.80,
                        token_total=1_200,
                        cost_eligible=True,
                    ),
                    _row(
                        valid,
                        "5V2",
                        200,
                        0.82,
                        token_total=1_800,
                        cost_eligible=True,
                    ),
                    _row(valid, "5Legacy", 300, 0.81),
                    _row(invalid, "5V1", 100, 0.80, cost_eligible=False),
                ],
                bench_version=9,
                created_at=_NOW,
            )

        costs = await confirmation_efficiency_costs_by_agent(
            session, agent_ids=[valid, invalid], bench_version=9
        )

        assert costs[valid].seed_token_totals == (1_200, 1_800)
        assert costs[valid].evidence_valid is True
        assert costs[invalid].seed_token_totals == ()
        assert costs[invalid].evidence_valid is False

    async def test_efficiency_costs_median_each_seed_across_validators(
        self, session: AsyncSession
    ) -> None:
        """One seed is one observation, whatever the validator count.

        Quality already medians a seed across validators, so cost must too:
        otherwise a seed three validators happened to draw would outweigh one a
        single validator drew, and a lone validator could set an agent's cost.
        """
        agent = await _seed_agent(session, "per-seed-median")
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    # Seed 100 drawn by three validators, one of them an outlier.
                    _row(
                        agent, "5V1", 100, 0.80, token_total=1_000, cost_eligible=True
                    ),
                    _row(
                        agent, "5V2", 100, 0.81, token_total=1_200, cost_eligible=True
                    ),
                    _row(
                        agent, "5V3", 100, 0.80, token_total=90_000, cost_eligible=True
                    ),
                    # Seed 200 drawn by one validator.
                    _row(
                        agent, "5V1", 200, 0.79, token_total=2_000, cost_eligible=True
                    ),
                ],
                bench_version=9,
                created_at=_NOW,
            )

        costs = await confirmation_efficiency_costs_by_agent(
            session, agent_ids=[agent], bench_version=9
        )

        # Two seeds -> two observations, ascending by seed. The 90k outlier is
        # medianed away rather than counted as a third sample.
        assert costs[agent].seed_token_totals == (1_200.0, 2_000.0)
        assert costs[agent].evidence_valid is True

    def test_only_complete_cohort_waves_are_fold_eligible(self) -> None:
        first, second, third = uuid4(), uuid4(), uuid4()
        assert completed_confirmation_wave_seeds(
            member_ids=[first, second, third],
            seeds_by_agent={
                first: [100, 200, 300],
                second: [100, 200],
                third: [100],
            },
        ) == frozenset({100})

    def test_a_zero_depth_entrant_empties_the_strict_intersection(self) -> None:
        """The 03:56Z incident, as a unit test.

        ``dittoLife-v1`` finalized, displaced ``banblackycat`` from the top five,
        and brought no confirmation rows with it. Under ``strict`` that single
        arrival erases eight waves of accumulated evidence for every other agent
        -- and not only on the display: ``official_composite`` reverts to the
        three-score quorum median, so validator weights revert with it.
        """
        deep, mid, entrant = uuid4(), uuid4(), uuid4()
        seeds = {deep: [100, 200, 300], mid: [100, 200, 300], entrant: []}

        strict = fold_eligible_seeds_by_agent(
            member_ids=[deep, mid, entrant], seeds_by_agent=seeds, mode="strict"
        )

        assert strict[deep] == frozenset()
        assert strict[mid] == frozenset()

    def test_participants_keeps_the_evidence_the_entrant_never_ran(self) -> None:
        """The recommended fix, on the same input.

        An agent at depth zero has never been leased any of these seeds, so it is
        not protecting a running lease. Excluding it preserves every completed
        wave, and the two agents that DO get a continual mean still share one
        identical seed set -- comparability is untouched.
        """
        deep, mid, entrant = uuid4(), uuid4(), uuid4()
        seeds = {deep: [100, 200, 300], mid: [100, 200, 300], entrant: []}

        participants = fold_eligible_seeds_by_agent(
            member_ids=[deep, mid, entrant],
            seeds_by_agent=seeds,
            mode="participants",
        )

        assert participants[deep] == frozenset({100, 200, 300})
        assert participants[mid] == frozenset({100, 200, 300})
        # Equal composition among everyone who receives a continual mean.
        assert participants[deep] == participants[mid]
        assert participants[entrant] == frozenset()

    def test_participants_still_waits_on_a_member_that_has_started(self) -> None:
        """Catch-up is preserved: a partial member still narrows the wave.

        This is the half of the strict rule that is genuinely protecting a
        running lease, and ``participants`` keeps it. Only depth ZERO is treated
        as "not in the lane yet".
        """
        deep, catching_up = uuid4(), uuid4()

        participants = fold_eligible_seeds_by_agent(
            member_ids=[deep, catching_up],
            seeds_by_agent={deep: [100, 200, 300], catching_up: [100]},
            mode="participants",
        )

        assert participants[deep] == frozenset({100})
        assert participants[catching_up] == frozenset({100})

    def test_per_agent_gives_every_agent_its_own_depth(self) -> None:
        """Peyton's literal ask, and the comparability it costs.

        Each agent keeps everything it ran. The means are then taken over
        different seed sets, which is exactly the seed-composition confound the
        shared-seed design exists to cancel -- hence the operator gate.
        """
        deep, mid, entrant = uuid4(), uuid4(), uuid4()

        per_agent = fold_eligible_seeds_by_agent(
            member_ids=[deep, mid, entrant],
            seeds_by_agent={deep: [100, 200, 300], mid: [100], entrant: []},
            mode="per_agent",
        )

        assert per_agent[deep] == frozenset({100, 200, 300})
        assert per_agent[mid] == frozenset({100})
        assert per_agent[entrant] == frozenset()

    def test_strict_is_the_default_and_matches_the_legacy_helper(self) -> None:
        """Merging this must not move a single composite."""
        first, second, third = uuid4(), uuid4(), uuid4()
        seeds = {first: [100, 200, 300], second: [100, 200], third: [100]}

        by_agent = fold_eligible_seeds_by_agent(
            member_ids=[first, second, third], seeds_by_agent=seeds
        )
        legacy = completed_confirmation_wave_seeds(
            member_ids=[first, second, third], seeds_by_agent=seeds
        )

        # Every member resolves to the same set the single shared value held,
        # so no composite moves when this ships.
        assert set(by_agent.values()) == {legacy}
        assert legacy == frozenset({100})

    async def test_composites_by_seed_medians_across_validators(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_agent(session)
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    _row(aid, "5V1", 100, 0.80),
                    _row(aid, "5V2", 100, 0.84),
                    _row(aid, "5V3", 100, 0.82),
                    _row(aid, "5V1", 200, 0.70),
                ],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
        by_seed = await confirmation_composites_by_seed(
            session, agent_ids=[aid], bench_version=_BENCH_VERSION
        )
        assert by_seed[aid][100] == 0.82  # median of 0.80/0.82/0.84
        assert by_seed[aid][200] == 0.70

    async def test_depth_counts_distinct_seeds(self, session: AsyncSession) -> None:
        aid = await _seed_agent(session)
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    _row(aid, "5V1", 100, 0.80),
                    _row(aid, "5V2", 100, 0.81),  # same seed, another validator
                    _row(aid, "5V1", 200, 0.82),
                    _row(aid, "5V1", 300, 0.83),
                ],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
        depths = await confirmation_depths(
            session, agent_ids=[aid], bench_version=_BENCH_VERSION
        )
        assert depths[aid] == 3  # three distinct seeds

    async def test_history_returns_raw_unaggregated_records(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_agent(session)
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[_row(aid, "5V1", 100, 0.80), _row(aid, "5V2", 100, 0.84)],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
        history = await confirmation_history_by_agent(
            session, agent_ids=[aid], bench_version=_BENCH_VERSION
        )
        rows = history[aid]
        # Raw per-(validator, seed) rows, NOT medianed: two rows for seed 100.
        assert len(rows) == 2
        assert {r.composite for r in rows} == {0.80, 0.84}
        assert all(r.bench_version == _BENCH_VERSION for r in rows)

    async def test_universe_is_every_recorded_seed_across_reigns(
        self, session: AsyncSession
    ) -> None:
        """The cumulative set the lane may lease, not one reign's anchor.

        Two agents carrying seeds from different champions: the universe is the
        union, deduplicated and seed-ordered, so an outgoing reign's coverage
        stays in scope as backlog. Version-scoped, because the anchor is keyed on
        the major bench version and seeds do not carry across one.
        """
        first = await _seed_agent(session, "first")
        second = await _seed_agent(session, "second")
        async with session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    _row(first, "5V1", 300, 0.80),
                    _row(first, "5V2", 300, 0.81),  # dedup across validators
                    _row(first, "5V1", 100, 0.82),
                    _row(second, "5V1", 200, 0.83),  # only the other agent has it
                ],
                bench_version=_BENCH_VERSION,
                created_at=_NOW,
            )
            await append_confirmation_scores(
                session,
                rows=[_row(first, "5V1", 999, 0.90)],
                bench_version=_NEXT_BENCH_VERSION,
                created_at=_NOW,
            )

        assert await lane_seed_universe(
            session, agent_ids=[first, second], bench_version=_BENCH_VERSION
        ) == (100, 200, 300)
        # A version the caller did not ask about contributes nothing.
        assert await lane_seed_universe(
            session, agent_ids=[first, second], bench_version=_NEXT_BENCH_VERSION
        ) == (999,)
        # And an agent outside the cohort contributes nothing.
        assert await lane_seed_universe(
            session, agent_ids=[second], bench_version=_BENCH_VERSION
        ) == (200,)

    async def test_universe_is_empty_without_agents(
        self, session: AsyncSession
    ) -> None:
        assert (
            await lane_seed_universe(
                session, agent_ids=[], bench_version=_BENCH_VERSION
            )
            == ()
        )

    async def test_absent_agents_map_to_empty(self, session: AsyncSession) -> None:
        assert (
            await confirmation_composites_by_seed(
                session, agent_ids=[], bench_version=_BENCH_VERSION
            )
            == {}
        )
        assert (
            await confirmation_depths(
                session, agent_ids=[uuid4()], bench_version=_BENCH_VERSION
            )
            == {}
        )


class TestConfirmationCatchupSeeds:
    """The coverage gap of one member measured against the rest of its wave."""

    _ANCHORED = [11, 22, 33, 44]

    def test_depth_zero_member_owes_everything_its_peers_settled(self) -> None:
        newcomer, *peers = (uuid4() for _ in range(4))
        seeds_by_agent = {peer: [11, 22, 33] for peer in peers}

        assert confirmation_catchup_seeds(
            member_id=newcomer,
            peer_ids=[newcomer, *peers],
            anchored_seeds=self._ANCHORED,
            seeds_by_agent=seeds_by_agent,
        ) == (11, 22, 33)

    def test_excludes_only_seeds_no_peer_has_reached(self) -> None:
        """Backlog is any peer's evidence; unreached seeds are wave growth.

        Seed 33 is held by one peer and not the other. That is still recorded
        evidence this member lacks, so it is backlog: one holder is sufficient
        reason to send everybody else to it. Reading the intersection here left
        such a seed invisible to catch-up -- nobody was ever sent to it, and it
        could not enter the fold either, so it stayed a permanent orphan.

        Seed 44 is missing for EVERYONE, and that is the line. Leasing it here
        would let one member run ahead of the wave; growth pacing owns it.
        """
        newcomer, peer_a, peer_b = (uuid4() for _ in range(3))

        assert confirmation_catchup_seeds(
            member_id=newcomer,
            peer_ids=[peer_a, peer_b],
            anchored_seeds=self._ANCHORED,
            seeds_by_agent={peer_a: [11, 22], peer_b: [11, 22, 33]},
        ) == (11, 22, 33)

    def test_a_single_peers_orphan_seed_becomes_everyone_elses_backlog(self) -> None:
        """The 2026-07-28 orphan case, reduced.

        A previous reign's seed survives on one agent and nowhere else. Under the
        intersection reading it was owed by nobody, so 188 of 288 recorded runs
        contributed to nothing. It has to come back as backlog for every member
        that lacks it, which is what lets the shared set grow across a dethrone
        instead of resetting.
        """
        holder, *others = (uuid4() for _ in range(4))
        stale = 33
        seeds_by_agent = {holder: [11, stale], **{other: [11] for other in others}}

        for other in others:
            assert confirmation_catchup_seeds(
                member_id=other,
                peer_ids=[holder, *others],
                anchored_seeds=self._ANCHORED,
                seeds_by_agent=seeds_by_agent,
            ) == (stale,)

    def test_omits_seeds_the_member_already_holds(self) -> None:
        member, peer = uuid4(), uuid4()

        assert confirmation_catchup_seeds(
            member_id=member,
            peer_ids=[peer],
            anchored_seeds=self._ANCHORED,
            seeds_by_agent={member: [11, 33], peer: [11, 22, 33]},
        ) == (22,)

    def test_caught_up_member_owes_nothing(self) -> None:
        member, peer = uuid4(), uuid4()

        assert (
            confirmation_catchup_seeds(
                member_id=member,
                peer_ids=[member, peer],
                anchored_seeds=self._ANCHORED,
                seeds_by_agent={member: [11, 22], peer: [11, 22]},
            )
            == ()
        )

    def test_a_lone_member_has_nothing_to_catch_up_to(self) -> None:
        member = uuid4()

        assert (
            confirmation_catchup_seeds(
                member_id=member,
                peer_ids=[member],
                anchored_seeds=self._ANCHORED,
                seeds_by_agent={},
            )
            == ()
        )

    def test_result_follows_champion_anchored_order(self) -> None:
        """Issuance order is the seed family's order, not set-iteration order."""
        member, peer = uuid4(), uuid4()

        assert confirmation_catchup_seeds(
            member_id=member,
            peer_ids=[peer],
            anchored_seeds=[44, 33, 22, 11],
            seeds_by_agent={peer: [11, 22, 33, 44]},
        ) == (44, 33, 22, 11)

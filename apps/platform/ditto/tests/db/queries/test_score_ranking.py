"""The ordering is canonical, or it is not an ordering.

Every surface that ranks submissions -- the public board's ``rank``, the
validator ledger, the KOTH fold, the queue's score floors, the efficiency
lineage dedupe -- reads :mod:`ditto.score_order`. These tests exist because the
previous arrangement (each surface restating the comparator, and two of them
disagreeing about which number "the score" is) told a miner he was below a
"fifth place" that named a different agent than the board did.

Three things are pinned here:

1. The comparator's tiebreaks, including the cases that only matter when two
   rows are otherwise identical.
2. That the SQL form and the Python form agree row for row. They are the same
   rule written twice, in two languages, and that is exactly the drift this
   module exists to prevent -- so it is asserted, not assumed.
3. That the queue's continuation floor is cut on ``official_composite``, the
   same score the board ranks by, on a board where the two candidate keys
   invert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import Agent, ValidatorHeartbeat, ValidatorTicket
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.score_ranking import official_composites
from ditto.db.queries.scores import list_eligible_ledger, upsert_score
from ditto.db.queries.tickets import get_score_priority_floor_rows
from ditto.score_order import rank_submissions, score_order_key

_BENCH = MIN_SCOREABLE_BENCH_VERSION
_BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_VALIDATORS = (
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
)


@dataclass(frozen=True)
class _Row:
    """The smallest thing the comparator will accept."""

    agent_id: UUID
    first_seen: datetime
    composite: float
    eligible: bool = True


@dataclass(frozen=True)
class _FinalRow:
    agent_id: UUID
    miner_hotkey: str
    first_seen: datetime
    composite: float
    bench_version: int = 7


def _uuid(nibble: str) -> UUID:
    return UUID(nibble * 32)


class TestComparator:
    def test_ranks_by_score_descending(self) -> None:
        low = _Row(_uuid("1"), _BASE, 0.10)
        high = _Row(_uuid("2"), _BASE, 0.90)
        assert rank_submissions([low, high]) == [high, low]

    def test_a_score_tie_is_broken_by_the_earlier_first_seen(self) -> None:
        later = _Row(_uuid("2"), _BASE + timedelta(days=1), 0.50)
        earlier = _Row(_uuid("1"), _BASE, 0.50)
        assert rank_submissions([later, earlier]) == [earlier, later]

    def test_a_first_seen_tie_is_broken_by_the_lower_agent_id(self) -> None:
        high_id = _Row(_uuid("f"), _BASE, 0.50)
        low_id = _Row(_uuid("0"), _BASE, 0.50)
        assert rank_submissions([high_id, low_id]) == [low_id, high_id]

    def test_an_unranked_row_sorts_below_every_ranked_row(self) -> None:
        """A smoke run can carry a high composite; it can never outrank a real
        one. The eligibility term leads the key precisely so that an inflated
        12-case run cannot take a leaderboard position or hold a queue floor."""
        smoke = _Row(_uuid("1"), _BASE, 0.99, eligible=False)
        real = _Row(_uuid("2"), _BASE, 0.10)
        assert rank_submissions([smoke, real]) == [real, smoke]

    def test_an_override_score_replaces_the_stored_composite(self) -> None:
        """``official_composite`` is supplied per agent, not stored on the row."""
        median_leader = _Row(_uuid("1"), _BASE, 0.90)
        mean_leader = _Row(_uuid("2"), _BASE, 0.80)
        official = {median_leader.agent_id: 0.75, mean_leader.agent_id: 0.875}
        assert rank_submissions([median_leader, mean_leader], scores=official) == [
            mean_leader,
            median_leader,
        ]

    def test_rows_without_an_eligible_flag_are_treated_as_ranked(self) -> None:
        """``KothEntry`` carries no eligibility flag -- the fold filters first."""

        @dataclass(frozen=True)
        class _Bare:
            agent_id: UUID
            first_seen: datetime
            composite: float

        low = _Bare(_uuid("1"), _BASE, 0.10)
        high = _Bare(_uuid("2"), _BASE, 0.90)
        assert rank_submissions([low, high]) == [high, low]

    def test_the_key_is_a_plain_tuple_so_it_composes(self) -> None:
        row = _Row(_uuid("3"), _BASE, 0.25)
        assert score_order_key(row) == (False, -0.25, _BASE, str(row.agent_id))
        assert score_order_key(row, score=0.5) == (
            False,
            -0.5,
            _BASE,
            str(row.agent_id),
        )

    def test_efficiency_bonus_folds_after_continual_mean(self) -> None:
        row = _FinalRow(_uuid("3"), "5" + "a" * 47, _BASE, 0.8)

        scores = official_composites(
            [row],
            quorum={row.agent_id: [0.7, 0.8, 0.9]},
            completed_waves={row.agent_id: {10: 0.6, 20: 0.9}},
            continual_mean_active=True,
            efficiency_bonuses={row.agent_id: 0.1},
            efficiency_fold_active=True,
        )

        assert scores[row.agent_id] == pytest.approx(0.78 * 1.1)


async def _seed(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    composites: tuple[float, ...],
    created_at: datetime,
    n: int = 114,
) -> UUID:
    """One finalized submission, with the accepted tickets its scores imply."""
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="agent",
                sha256="ab" * 32,
                size_bytes=524288,
                status=AgentStatus.SCORED,
                created_at=created_at,
            )
        )
        await session.flush()
        for index, composite in enumerate(composites):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=_VALIDATORS[index],
                run_id=f"{hotkey[:8]}-{index}",
                seed=987654321,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=n,
                generated_at=_BASE + timedelta(minutes=index),
                signature="ab" * 64,
                details={"bench_version": _BENCH},
                bench_version=_BENCH,
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=_VALIDATORS[index],
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=_BASE + timedelta(minutes=index),
                    deadline=_BASE + timedelta(minutes=index + 90),
                    attempt_count=1,
                    manual_retry_grants=0,
                )
            )
    return agent_id


@pytest.mark.asyncio
class TestSqlMatchesPython:
    async def test_ledger_sql_order_reproduces_the_python_comparator(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The one place the rule is written twice, checked against itself.

        The fixture is built out of the cases where a comparator can silently
        drift: an exact composite tie resolved by age, a same-instant tie
        resolved by ``agent_id``, and an unranked run whose composite outranks
        every real one.
        """
        await _seed(
            session_maker,
            hotkey="5" + "A" * 47,
            composites=(0.90, 0.90, 0.90),
            created_at=_BASE,
        )
        # Exact score tie, different ages.
        await _seed(
            session_maker,
            hotkey="5" + "B" * 47,
            composites=(0.70, 0.70, 0.70),
            created_at=_BASE + timedelta(days=2),
        )
        await _seed(
            session_maker,
            hotkey="5" + "C" * 47,
            composites=(0.70, 0.70, 0.70),
            created_at=_BASE + timedelta(days=1),
        )
        # Same score AND same instant: only agent_id can separate these.
        for marker in ("D", "E", "F"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=(0.50, 0.50, 0.50),
                created_at=_BASE + timedelta(days=3),
            )
        # A 12-case smoke run scoring higher than anything real.
        await _seed(
            session_maker,
            hotkey="5" + "G" * 47,
            composites=(0.99, 0.99, 0.99),
            created_at=_BASE,
            n=12,
        )

        async with session_maker() as session:
            rows = await list_eligible_ledger(session, bench_version=_BENCH)

        assert len(rows) == 7
        assert rows == rank_submissions(rows)
        # The fixture actually exercises the tiebreaks, or the assertion above
        # is satisfied by an ordering that never had to break a tie.
        assert len({row.composite for row in rows}) < len(rows)
        assert rows[-1].eligible is False
        assert rows[-1].composite == pytest.approx(0.99)


@pytest.mark.asyncio
class TestContinuationFloor:
    async def test_floor_is_cut_by_official_composite(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Six finalized owners, built so the two candidate keys invert.

        By raw ``composite`` fifth place is "E" at 0.82. "F" is last at 0.80 but
        completes waves at 0.95, so its ``official_composite`` is 0.875 and it
        sits third -- which pushes fifth place onto "D" at 0.84. The floor is a
        gate on whether a submission can still reach the emission set, and
        emission-set membership is decided by ``official_composite``, so 0.84 is
        the number that gate has to use.
        """
        by_marker: dict[str, UUID] = {}
        for rank, marker in enumerate("ABCDEF"):
            by_marker[marker] = await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )

        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATORS[0],
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities={
                        "screened_images": True,
                        "require_screened_image": True,
                        "source_build_fallback": False,
                        "full_stack_managed": True,
                        "stack_updater": True,
                        "sandbox_egress_restricted": True,
                        "ticket_inference": False,
                        "signed_score_quorum": False,
                        "executor_isolation": "ephemeral_vm",
                        "scorer_benchmarks": {
                            "status": "fresh_verified",
                            "supported_bench_versions": [_BENCH],
                            "observed_at": int(now.timestamp()),
                            "software_version": "1.0.0",
                            "source_revision": "a" * 40,
                        },
                    },
                )
            )
            # A wave only counts once every cohort member has scored that seed,
            # so all six retest; everyone but "F" retests at its own composite.
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        agent_id,
                        "5V1",
                        seed,
                        0.95 if marker == "F" else 0.90 - index * 0.02,
                        f"r-{marker}-{seed}",
                        None,
                    )
                    for index, (marker, agent_id) in enumerate(by_marker.items())
                    for seed in (100, 200, 300)
                ],
                bench_version=_BENCH,
                created_at=now,
            )

        async with session_maker() as session:
            continuation, _provisional = await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            )

        assert continuation is not None
        assert continuation.row.agent_id == by_marker["D"]
        assert continuation.score == pytest.approx(0.84)
        # The retired raw-composite cut is absent, not merely different.
        assert continuation.row.agent_id != by_marker["E"]
        assert continuation.score != pytest.approx(0.82)

    async def test_floor_read_does_not_hydrate_score_telemetry(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The allocator floor read must stay scalar-only.

        Every ordinary idle-slot claim reaches this read, and it consumes
        nothing but ranking fields. Hydrating `details` detoasted and decoded
        the full per-case audit blob for every eligible row on every claim and
        then discarded it -- user CPU burned on the single API worker's event
        loop, which is the shape production saturated on (#388).
        """
        for rank, marker in enumerate("ABCDEF"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )

        async with session_maker() as session:
            continuation, provisional = await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            )

        assert continuation is not None
        # None here means the column was never selected, not that the row has
        # no telemetry: these seeded scores carry details.
        assert continuation.row.details is None
        if provisional is not None:
            assert provisional.row.details is None

    async def test_no_floor_below_five_finalized_owners(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        for rank, marker in enumerate("ABCD"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )
        async with session_maker() as session:
            assert await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            ) == (None, None)


# A comparator over ledger rows: a negated score, then ``first_seen``, then
# ``agent_id``. Matching this outside ``ditto/score_order.py`` means someone has
# started a second ordering, which is the failure mode this whole module exists
# to prevent.
_HAND_ROLLED_COMPARATOR = re.compile(
    r"-\s*\w+(\.\w+)*\.composite\s*,\s*\n?\s*\w+(\.\w+)*\.first_seen"
    r"|-\s*effective_composite\(\w+\)\s*,\s*\n?\s*\w+\.first_seen",
)


def test_the_comparator_has_exactly_one_implementation() -> None:
    """No second ordering may reappear.

    The operator's rule is that each thing we maintain has one canonical
    version. The comparator now lives in ``ditto/score_order.py`` (Python and
    SQL side by side) and callers reach it through ``rank_submissions`` /
    ``score_order_terms``. A hand-rolled ``(-composite, first_seen, agent_id)``
    anywhere else is the beginning of the divergence again, so it fails here
    rather than in a miner's support ticket.
    """
    root = Path(__file__).resolve().parents[3]
    canonical = root / "score_order.py"
    assert canonical.is_file(), canonical

    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != canonical
        and "tests" not in path.parts
        and _HAND_ROLLED_COMPARATOR.search(path.read_text())
    )
    assert offenders == [], (
        "hand-rolled score comparator outside ditto/score_order.py: "
        + ", ".join(offenders)
    )

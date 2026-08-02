"""Retirement eligibility, and its interaction with previous-generation carryover.

The carryover cases are the reason this file exists. Retiring a stranded
submission and adopting it into the new era are opposite remedies for the exact
same rows, so the two mechanisms have to agree on precedence. If they ever
disagree, enabling the carryover policy flag would silently resurrect work an
operator had already closed out under their own name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import PrevGenCarryoverSettings
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    Score,
    SubmissionRetirement,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_carryover import stranded_prev_gen_candidates
from ditto.db.queries.retirement import (
    classify_population,
    retirement_gate,
)
from ditto.db.queries.scores import SCORING_QUORUM

_ROLLOUT_START = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_NOW = _ROLLOUT_START + timedelta(days=3)
# The generation being retired and the one superseding it. These used to be
# 6 and 7 -- the transition that was live when the file was written -- and the
# tests below write real ``scores`` rows at ``_FROM_VERSION``, which the floor
# now refuses under MIN_SCOREABLE_BENCH_VERSION. Nothing here is about v6
# specifically: the rule under test is "the generation the fleet has moved off,
# whichever that is", so the same pair sits one era up.
_FROM_VERSION = 7
_DESIRED_VERSION = 8
_ENABLED = PrevGenCarryoverSettings(enabled=True)


async def _seed_rollout(session: AsyncSession) -> BenchmarkRollout:
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM_VERSION,
        desired_version=_DESIRED_VERSION,
        status="collecting",
        cohort_size=10,
        created_at=_ROLLOUT_START,
        rescore_cohort_target=10,
        priority_cohort_target=5,
    )
    session.add(rollout)
    await session.flush()
    return rollout


async def _seed_stranded(
    session: AsyncSession, *, name: str, score_count: int = 2
) -> UUID:
    agent_id = uuid4()
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=f"5Miner-{name}",
        name=name,
        version=1,
        sha256=f"{abs(hash(name)) % (16**64):064x}",
        status=AgentStatus.EVALUATING,
        screening_policy_version=SCREENING_POLICY_VERSION,
        created_at=_ROLLOUT_START - timedelta(days=4),
    )
    agent.screened_image_sha256 = "12" * 32
    agent.screened_image_size_bytes = 123
    agent.screened_image_id = "sha256:" + "34" * 32
    agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = _ROLLOUT_START
    session.add(agent)
    await session.flush()
    for index in range(score_count):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM_VERSION,
                validator_hotkey=f"5Validator-{index}",
                run_id=f"{name}-{index}",
                seed=7,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=114,
                generated_at=_ROLLOUT_START - timedelta(days=4),
            )
        )
    await session.flush()
    return agent_id


def _retire(session: AsyncSession, agent_id: UUID) -> None:
    session.add(
        SubmissionRetirement(
            retirement_id=uuid4(),
            agent_id=agent_id,
            bench_version=_FROM_VERSION,
            superseded_by_version=_DESIRED_VERSION,
            actor="peyton",
            reason=f"benchmark v{_FROM_VERSION} is closed and will not be scored again",
            expected_snapshot="ab" * 32,
            score_count=2,
            ticket_snapshot=[],
            created_at=_NOW,
        )
    )


class TestCarryoverInteraction:
    async def test_retired_submission_is_never_adopted_by_carryover(
        self, session: AsyncSession
    ) -> None:
        """Turning the carryover flag on must not resurrect retired work."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            retired = await _seed_stranded(session, name="retired-one")
            live = await _seed_stranded(session, name="still-stranded")
            _retire(session, retired)

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )

        assert [candidate.agent_id for candidate in selected] == [live]

    async def test_an_unretired_peer_is_still_adopted(
        self, session: AsyncSession
    ) -> None:
        """The exclusion is scoped to the retired row, not the whole cohort."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            first = await _seed_stranded(session, name="peer-a")
            second = await _seed_stranded(session, name="peer-b")

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )

        assert {candidate.agent_id for candidate in selected} == {first, second}


class TestPopulationClassification:
    @pytest.mark.parametrize(
        ("score_count", "expected"),
        [
            (0, "never_scored"),
            (1, "partially_scored"),
            (SCORING_QUORUM - 1, "partially_scored"),
            (SCORING_QUORUM, "finalized"),
        ],
    )
    def test_populations_split_on_quorum(self, score_count: int, expected: str) -> None:
        assert classify_population(score_count) == expected


class TestRetirementGate:
    """The gate is pure, so these are the cheapest place to pin the rule."""

    def _agent(self, status: AgentStatus = AgentStatus.EVALUATING) -> Agent:
        return Agent(
            agent_id=uuid4(),
            miner_hotkey="5Miner",
            name="gate",
            version=1,
            sha256="ab" * 32,
            status=status,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=_ROLLOUT_START,
        )

    def _score(self) -> Score:
        return Score(
            agent_id=uuid4(),
            bench_version=_FROM_VERSION,
            validator_hotkey="5Validator",
            run_id="run",
            seed=7,
            composite=0.5,
            tool_mean=0.5,
            memory_mean=0.5,
            median_ms=1,
            n=114,
            generated_at=_ROLLOUT_START,
        )

    def _verdict(self, **overrides: object):
        kwargs: dict = {
            "agent": self._agent(),
            "scores": [self._score(), self._score()],
            "tickets": [],
            "bench_version": _FROM_VERSION,
            "active_version": _DESIRED_VERSION,
            "admitted_to_active_era": False,
            "already_retired": False,
            "already_withdrawn": False,
        }
        kwargs.update(overrides)
        return retirement_gate(**kwargs)  # type: ignore[arg-type]

    def test_a_closed_generation_submission_below_quorum_is_eligible(self) -> None:
        verdict = self._verdict()
        assert verdict.allowed is True
        assert verdict.reason is None
        assert verdict.population == "partially_scored"

    def test_never_ticketed_work_is_eligible_and_labelled_honestly(self) -> None:
        verdict = self._verdict(scores=[])
        assert verdict.allowed is True
        assert verdict.population == "never_scored"

    def test_current_generation_is_refused(self) -> None:
        verdict = self._verdict(bench_version=_DESIRED_VERSION)
        assert verdict.allowed is False
        assert verdict.reason == "submission is queued against the active benchmark"

    def test_admission_to_the_active_era_is_refused(self) -> None:
        verdict = self._verdict(admitted_to_active_era=True)
        assert verdict.allowed is False
        assert verdict.reason == "submission is admitted to the active benchmark"

    def test_finalized_work_is_refused(self) -> None:
        verdict = self._verdict(scores=[self._score() for _ in range(SCORING_QUORUM)])
        assert verdict.allowed is False
        assert verdict.reason == "submission already reached scoring quorum"
        assert verdict.population == "finalized"

    def test_a_live_ticket_is_refused(self) -> None:
        ticket = ValidatorTicket(
            agent_id=uuid4(),
            validator_hotkey="5Validator",
            status=TicketStatus.ISSUED,
            issued_at=_ROLLOUT_START,
            deadline=_NOW + timedelta(hours=1),
            bench_version=_FROM_VERSION,
            attempt_count=1,
        )
        verdict = self._verdict(tickets=[ticket])
        assert verdict.allowed is False
        assert verdict.reason == "a validator ticket is still active"

    def test_an_already_withdrawn_submission_is_refused(self) -> None:
        verdict = self._verdict(already_withdrawn=True)
        assert verdict.allowed is False

    def test_a_non_evaluating_submission_is_refused(self) -> None:
        verdict = self._verdict(agent=self._agent(status=AgentStatus.REJECTED))
        assert verdict.allowed is False
        assert verdict.reason == "submission is not waiting for validator scores"

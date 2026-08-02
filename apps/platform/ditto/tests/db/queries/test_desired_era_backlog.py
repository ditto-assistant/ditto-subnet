"""The fleet-wide gate that makes previous-generation work strictly last.

Lane position was never the whole answer. ``request_job`` already reaches the
carryover and source-backfill lanes only after every desired-version lane has
returned nothing, and that is exactly what made the bug hard to see: "nothing"
is a *per-validator* answer. These cases pin the difference -- a queue that is
deep but invisible to the polling validator must still hold the previous
generation back -- and, just as importantly, pin the cases where it must not,
so a backlog nobody can act on cannot starve the retired era forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.desired_era_backlog import (
    desired_era_work_outstanding,
    prev_generation_agent_ids,
)
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION

_ROLLOUT_START = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
_NOW = _ROLLOUT_START + timedelta(days=2)
_FROM_VERSION = 6
_DESIRED_VERSION = 7
_FLEET = ("5ValidatorA", "5ValidatorB", "5ValidatorC")


async def _seed_rollout(
    session: AsyncSession, *, status: str = "collecting"
) -> BenchmarkRollout:
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM_VERSION,
        desired_version=_DESIRED_VERSION,
        status=status,
        cohort_size=10,
        created_at=_ROLLOUT_START,
        activated_at=_ROLLOUT_START if status == "activated" else None,
        rescore_cohort_target=10,
        priority_cohort_target=5,
    )
    session.add(rollout)
    await session.flush()
    return rollout


async def _seed_desired_era_agent(
    session: AsyncSession,
    *,
    name: str,
    created_at: datetime | None = None,
) -> UUID:
    """A fresh, fully eligible desired-era submission with nothing done to it."""
    agent_id = uuid4()
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=f"5Miner-{name}",
        name=name,
        sha256=f"{abs(hash(name)) % (16**64):064x}",
        status=AgentStatus.EVALUATING,
        screening_policy_version=SCREENING_POLICY_VERSION,
        created_at=created_at or (_ROLLOUT_START + timedelta(days=1)),
    )
    agent.screened_image_sha256 = "12" * 32
    agent.screened_image_size_bytes = 123
    agent.screened_image_id = "sha256:" + "34" * 32
    agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = _ROLLOUT_START
    session.add(agent)
    session.add(
        BenchmarkDataset(
            agent_id=agent_id,
            bench_version=_DESIRED_VERSION,
            seed=7,
            sha256="ab" * 32,
            run_size="full",
        )
    )
    await session.flush()
    return agent_id


def _ticket(
    *,
    agent_id: UUID,
    validator_hotkey: str,
    status: TicketStatus,
    attempt_count: int = 1,
    infra_retry_grants: int = 0,
    retry_after: datetime | None = None,
    deadline: datetime | None = None,
) -> ValidatorTicket:
    return ValidatorTicket(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        slot_id="slot-0",
        status=status,
        issued_at=_NOW - timedelta(hours=1),
        deadline=deadline or (_NOW + timedelta(hours=1)),
        bench_version=_DESIRED_VERSION,
        attempt_count=attempt_count,
        manual_retry_grants=0,
        infra_retry_grants=infra_retry_grants,
        retry_after=retry_after,
    )


class TestDesiredEraWorkOutstanding:
    async def test_untouched_submission_holds_the_gate_shut(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            await _seed_desired_era_agent(session, name="fresh")

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is True
        )

    async def test_empty_desired_era_queue_opens_the_gate(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )

    async def test_work_invisible_to_one_validator_still_holds_the_gate(
        self, session: AsyncSession
    ) -> None:
        """The bug in one case: ``issue_ticket`` says no, the fleet says yes.

        One validator already holds this submission's lease, so its own poll
        returns nothing and it used to go lease retired-era work. Two other
        validators still owe it a score, and it is their capacity the retired
        lease was consuming.
        """
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="one-lease")
            session.add(
                _ticket(
                    agent_id=agent_id,
                    validator_hotkey=_FLEET[0],
                    status=TicketStatus.ISSUED,
                )
            )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is True
        )

    async def test_fully_leased_quorum_is_not_outstanding(
        self, session: AsyncSession
    ) -> None:
        """Covered work is not waiting work, however recently it was covered."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="covered")
            for hotkey in _FLEET:
                session.add(
                    _ticket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.ISSUED,
                    )
                )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )

    async def test_retry_exhausted_backlog_does_not_starve_the_retired_era(
        self, session: AsyncSession
    ) -> None:
        """ "Deprioritized" must not quietly become "never".

        A submission every capable validator has burned its retry budget on is
        queue depth that no scheduler change can drain -- it needs an operator.
        Letting it hold the gate shut would hang the previous generation out to
        dry, which is precisely what this policy is not.
        """
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="exhausted")
            for hotkey in _FLEET:
                session.add(
                    _ticket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.EXPIRED,
                        attempt_count=MAX_ATTEMPTS_PER_VERSION,
                        deadline=_NOW - timedelta(hours=1),
                    )
                )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )

    async def test_one_unexhausted_validator_is_enough_to_hold_the_gate(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="partly-burnt")
            for hotkey in _FLEET[:2]:
                session.add(
                    _ticket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.EXPIRED,
                        attempt_count=MAX_ATTEMPTS_PER_VERSION,
                        deadline=_NOW - timedelta(hours=1),
                    )
                )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is True
        )

    async def test_cooldown_is_temporary_and_holds_the_gate_after_it_lapses(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="cooling")
            for hotkey in _FLEET:
                session.add(
                    _ticket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        status=TicketStatus.EXPIRED,
                        infra_retry_grants=1,
                        retry_after=_NOW + timedelta(hours=1),
                        deadline=_NOW - timedelta(hours=1),
                    )
                )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )
        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW + timedelta(hours=2),
                capable_validator_hotkeys=_FLEET,
            )
            is True
        )

    async def test_previous_generation_rows_are_not_their_own_justification(
        self, session: AsyncSession
    ) -> None:
        """A stranded row must not count as the desired-era work blocking itself."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            await _seed_desired_era_agent(
                session,
                name="stranded",
                created_at=_ROLLOUT_START - timedelta(days=5),
            )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )

    async def test_withdrawn_submission_is_not_outstanding(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_desired_era_agent(session, name="withdrawn")
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=_DESIRED_VERSION,
                    reason="miner requested withdrawal",
                    actor="admin_api",
                    expected_snapshot="0/3",
                    score_count=0,
                    ticket_snapshot=[],
                )
            )

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=_FLEET,
            )
            is False
        )

    async def test_no_capable_fleet_does_not_wedge_the_queue(
        self, session: AsyncSession
    ) -> None:
        """Nobody can serve the new era, so nothing is being crowded out."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            await _seed_desired_era_agent(session, name="fresh")

        assert (
            await desired_era_work_outstanding(
                session,
                rollout=rollout,
                now=_NOW,
                capable_validator_hotkeys=(),
            )
            is False
        )


class TestPrevGenerationAgentIds:
    async def test_pre_rollout_rows_are_previous_generation(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            await _seed_rollout(session)
            stranded = await _seed_desired_era_agent(
                session,
                name="stranded",
                created_at=_ROLLOUT_START - timedelta(days=5),
            )
            fresh = await _seed_desired_era_agent(session, name="fresh")

        assert await prev_generation_agent_ids(
            session,
            bench_version=_DESIRED_VERSION,
            agent_ids=[stranded, fresh],
        ) == {stranded}

    async def test_no_rollout_for_the_era_classifies_nothing(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_id = await _seed_desired_era_agent(
                session,
                name="orphan",
                created_at=_ROLLOUT_START - timedelta(days=5),
            )

        assert (
            await prev_generation_agent_ids(
                session, bench_version=_DESIRED_VERSION, agent_ids=[agent_id]
            )
            == set()
        )

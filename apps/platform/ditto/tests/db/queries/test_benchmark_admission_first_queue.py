"""Admission of submissions whose first queue entry came after a rollout opened.

A submission uploaded before a rollout but still in screening when it opened
(an infrastructure failure, an operator retry, a quarantine review) was never
in the previous era's queue. Before the first-queued-in-era disjunct it was
admitted by no rule at all once the rollout activated: not received in the new
era, not a frozen cohort member, not carryover. It sat ``evaluating`` with a
desired-version dataset and no validator would ever lease it.

The negative cases matter as much as the positive ones. The era boundary exists
so that historical work -- scored, or already waiting in the previous era's
queue -- does not re-enter the new queue through a rescreen, and the top-N
rescore cohort stays the only way scored work is retested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import PrevGenCarryoverSettings
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    Score,
    ScreeningAttempt,
    ScreeningQuarantine,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import (
    agent_is_admitted,
    benchmark_admission_predicate,
)
from ditto.db.queries.benchmark_carryover import stranded_prev_gen_candidates
from ditto.db.queries.desired_era_backlog import prev_generation_agent_ids
from ditto.db.queries.tickets import issue_ticket
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

_FROM_VERSION = 13
_DESIRED_VERSION = 14
_ROLLOUT_START = datetime(2026, 10, 28, 0, 30, 0, tzinfo=UTC)
_ACTIVATED_AT = _ROLLOUT_START + timedelta(hours=1)
_NOW = _ROLLOUT_START + timedelta(hours=6)
_TTL = timedelta(minutes=90)
_BEFORE = _ROLLOUT_START - timedelta(days=2)
_IN_WINDOW = _ROLLOUT_START + timedelta(minutes=30)
_AFTER = _ACTIVATED_AT + timedelta(hours=2)


async def _seed_rollout(
    session: AsyncSession, *, status: str = "activated"
) -> BenchmarkRollout:
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM_VERSION,
        desired_version=_DESIRED_VERSION,
        status=status,
        cohort_size=10,
        created_at=_ROLLOUT_START,
        activated_at=_ACTIVATED_AT if status == "activated" else None,
        rescore_cohort_target=10,
        priority_cohort_target=5,
    )
    session.add(rollout)
    await session.flush()
    return rollout


async def _seed_agent(
    session: AsyncSession,
    *,
    name: str,
    created_at: datetime = _BEFORE,
    status: AgentStatus = AgentStatus.EVALUATING,
    desired_dataset: bool = True,
) -> UUID:
    """A pre-rollout upload, screened-image complete, with no screening history."""
    agent_id = uuid4()
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=f"5Miner-{name}",
        name=name,
        sha256=f"{abs(hash(name)) % (16**64):064x}",
        status=status,
        screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
        created_at=created_at,
    )
    agent.screened_image_sha256 = "12" * 32
    agent.screened_image_size_bytes = 123
    agent.screened_image_id = "sha256:" + "34" * 32
    agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = created_at
    session.add(agent)
    await session.flush()
    if desired_dataset:
        session.add(
            BenchmarkDataset(
                agent_id=agent_id,
                bench_version=_DESIRED_VERSION,
                seed=7,
                sha256="ab" * 32,
                run_size="full",
                created_at=_NOW,
            )
        )
        await session.flush()
    return agent_id


async def _attempt(
    session: AsyncSession,
    agent_id: UUID,
    *,
    status: str,
    finished_at: datetime,
    build_only: bool = False,
) -> UUID:
    attempt_id = uuid4()
    session.add(
        ScreeningAttempt(
            attempt_id=attempt_id,
            agent_id=agent_id,
            screener_hotkey="5Screener",
            policy_version=SCREENING_FLOOR_POLICY_VERSION,
            status=status,
            started_at=finished_at - timedelta(minutes=40),
            deadline=finished_at + timedelta(minutes=20),
            finished_at=finished_at,
            build_only=build_only,
        )
    )
    await session.flush()
    return attempt_id


async def _released_quarantine(
    session: AsyncSession,
    agent_id: UUID,
    *,
    quarantined_at: datetime,
    released_at: datetime,
) -> None:
    attempt_id = await _attempt(
        session, agent_id, status="quarantined", finished_at=quarantined_at
    )
    session.add(
        ScreeningQuarantine(
            quarantine_id=uuid4(),
            agent_id=agent_id,
            attempt_id=attempt_id,
            screener_hotkey="5Screener",
            policy_version=SCREENING_FLOOR_POLICY_VERSION,
            manifest_digest="0" * 64,
            reason_code="l2-model-inconclusive",
            status="resolved",
            created_at=quarantined_at,
            resolved_at=released_at,
            resolved_by="operator",
            resolution="release",
            resolution_reason="released after review",
        )
    )
    await session.flush()


async def _previous_era_score(session: AsyncSession, agent_id: UUID) -> None:
    for index in range(3):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM_VERSION,
                validator_hotkey=f"5Validator-{index}",
                run_id=f"{agent_id}-{index}",
                signature=None,
                seed=7,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=114,
                details={"bench_version": _FROM_VERSION},
                generated_at=_BEFORE + timedelta(hours=3),
            )
        )
    await session.flush()


async def _previous_era_ticket(session: AsyncSession, agent_id: UUID) -> None:
    session.add(
        ValidatorTicket(
            agent_id=agent_id,
            bench_version=_FROM_VERSION,
            validator_hotkey="5Validator-window",
            status=TicketStatus.EXPIRED,
            purpose=TicketPurpose.CANONICAL_QUORUM,
            issued_at=_IN_WINDOW,
            deadline=_IN_WINDOW + _TTL,
            attempt_count=1,
        )
    )
    await session.flush()


async def _admitted(session: AsyncSession, rollout: BenchmarkRollout) -> set[UUID]:
    return set(
        await session.scalars(
            select(Agent.agent_id).where(
                benchmark_admission_predicate(
                    rollout=rollout, bench_version=_DESIRED_VERSION
                )
            )
        )
    )


async def _lease(session: AsyncSession, agent_id: UUID) -> ValidatorTicket | None:
    return await issue_ticket(
        session,
        validator_hotkey="5Validator-v14",
        now=_NOW,
        ttl=_TTL,
        bench_version=_DESIRED_VERSION,
        artifact_mode="screened_only",
        only_agent_ids=[agent_id],
    )


class TestFirstQueuedAfterTheRolloutOpenedIsAdmitted:
    async def test_retried_screening_passing_after_activation_is_leased(
        self, session: AsyncSession
    ) -> None:
        """The production case: uploaded before, screening expired, retried after."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="retried")
            await _attempt(
                session,
                agent_id,
                status="expired",
                finished_at=_BEFORE + timedelta(hours=1),
            )
            await _attempt(session, agent_id, status="passed", finished_at=_AFTER)

        async with session.begin():
            assert agent_id in await _admitted(session, rollout)
            assert await agent_is_admitted(
                session, bench_version=_DESIRED_VERSION, agent_id=agent_id
            )
            ticket = await _lease(session, agent_id)
        assert ticket is not None
        assert ticket.agent_id == agent_id
        assert ticket.bench_version == _DESIRED_VERSION

    async def test_quarantine_released_after_the_rollout_opened(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="released")
            await _released_quarantine(
                session,
                agent_id,
                quarantined_at=_BEFORE + timedelta(hours=1),
                released_at=_AFTER,
            )

        async with session.begin():
            assert agent_id in await _admitted(session, rollout)
            ticket = await _lease(session, agent_id)
        assert ticket is not None
        assert ticket.agent_id == agent_id

    async def test_passed_inside_the_rollout_window_with_no_tickets(
        self, session: AsyncSession
    ) -> None:
        """Queued in the window, activation came before any ticket did."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(
                session, name="window", created_at=_ROLLOUT_START - timedelta(minutes=5)
            )
            await _attempt(session, agent_id, status="passed", finished_at=_IN_WINDOW)

        async with session.begin():
            assert agent_id in await _admitted(session, rollout)

    async def test_also_admitted_while_the_rollout_is_still_collecting(
        self, session: AsyncSession
    ) -> None:
        """Ledger authority can move before the rollout row says activated."""
        async with session.begin():
            await _seed_rollout(session, status="collecting")
            agent_id = await _seed_agent(session, name="collecting")
            await _attempt(session, agent_id, status="passed", finished_at=_AFTER)

        async with session.begin():
            assert await agent_is_admitted(
                session, bench_version=_DESIRED_VERSION, agent_id=agent_id
            )

    async def test_admitted_work_is_not_ranked_as_previous_generation(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            in_flight = await _seed_agent(session, name="in-flight")
            await _attempt(session, in_flight, status="passed", finished_at=_AFTER)
            historical = await _seed_agent(session, name="historical")
            await _attempt(session, historical, status="passed", finished_at=_BEFORE)

        async with session.begin():
            assert await prev_generation_agent_ids(
                session,
                bench_version=_DESIRED_VERSION,
                agent_ids=[in_flight, historical],
                rollout=rollout,
            ) == {historical}

    async def test_carryover_does_not_spend_a_slot_on_it(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session, status="collecting")
            agent_id = await _seed_agent(
                session, name="carry", status=AgentStatus.EVALUATING
            )
            await _attempt(session, agent_id, status="passed", finished_at=_IN_WINDOW)

        async with session.begin():
            candidates = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=0),
                now=_NOW,
            )
        assert agent_id not in {candidate.agent_id for candidate in candidates}


class TestHistoricalWorkStaysOutsideTheNewEra:
    async def test_scored_submission_rescreened_after_activation(
        self, session: AsyncSession
    ) -> None:
        """A policy rescreen regenerates a dataset; it must not admit."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(
                session, name="scored", status=AgentStatus.EVALUATING
            )
            await _attempt(session, agent_id, status="passed", finished_at=_BEFORE)
            await _previous_era_score(session, agent_id)
            await _attempt(session, agent_id, status="passed", finished_at=_AFTER)

        async with session.begin():
            assert agent_id not in await _admitted(session, rollout)
            assert await _lease(session, agent_id) is None

    async def test_waiting_in_the_previous_queue_before_the_rollout(
        self, session: AsyncSession
    ) -> None:
        """Never ticketed but queued before: the carryover policy's call."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="queued-before")
            await _attempt(session, agent_id, status="passed", finished_at=_BEFORE)
            # A later build-only rebuild is not a queue entry.
            await _attempt(
                session, agent_id, status="passed", finished_at=_AFTER, build_only=True
            )

        async with session.begin():
            assert agent_id not in await _admitted(session, rollout)
            assert await _lease(session, agent_id) is None

    async def test_released_before_the_rollout(self, session: AsyncSession) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="released-before")
            await _released_quarantine(
                session,
                agent_id,
                quarantined_at=_BEFORE,
                released_at=_BEFORE + timedelta(hours=2),
            )
            await _attempt(session, agent_id, status="passed", finished_at=_AFTER)

        async with session.begin():
            assert agent_id not in await _admitted(session, rollout)

    async def test_previous_era_ticket_in_the_window(
        self, session: AsyncSession
    ) -> None:
        """Partially served by the previous era: carryover, not this rule."""
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="window-ticket")
            await _attempt(session, agent_id, status="passed", finished_at=_IN_WINDOW)
            await _previous_era_ticket(session, agent_id)

        async with session.begin():
            assert agent_id not in await _admitted(session, rollout)

    async def test_no_screening_history_is_not_evidence(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            rollout = await _seed_rollout(session)
            agent_id = await _seed_agent(session, name="no-history")

        async with session.begin():
            assert agent_id not in await _admitted(session, rollout)
            assert await _lease(session, agent_id) is None

    async def test_admission_does_not_lease_a_rejected_submission(
        self, session: AsyncSession
    ) -> None:
        """Admission is one gate; issuance still requires ``evaluating``."""
        async with session.begin():
            await _seed_rollout(session)
            agent_id = await _seed_agent(
                session, name="rejected", status=AgentStatus.REJECTED
            )
            await _attempt(session, agent_id, status="passed", finished_at=_AFTER)

        async with session.begin():
            assert await _lease(session, agent_id) is None

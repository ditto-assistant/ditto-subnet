"""What a score submission is allowed to lock on its way out.

``activate_next_score_retest`` used to run at the tail of every ``submit_score``
transaction, long after that transaction had taken the agent row and its own
ticket row. It now belongs only to job issuance and explicit admin queue actions,
where its validator-wide serialization is the primary lock order. These query-
level tests retain the narrower guarantee that callers with no re-test lifecycle
also return before touching either lock.

That is a lock-order inversion, and it is not theoretical. Ticket issuance
guards a narrower key (``validator:slot``), so the two paths do not exclude each
other; a score submission holding ticket A and reaching for ticket B meets a job
request holding ticket B and reaching for agent A, and Postgres breaks the cycle
by aborting one of them. On this path the abort is an unhandled 500 that
discards a finished 90-minute benchmark run and, because ditto-subnet reports a
platform ``PlatformError`` as ``scoring_error``, bills the miner an attempt for
our own deadlock. Prod saw 40 of these in four days, one of which took a
legitimate competitor to ``retry_budget_exhausted`` one score short of quorum.

So the rule these tests encode is about the *absence* of locks, which is the
kind of property that silently regresses: a validator with no re-test lifecycle
must complete this call without touching either lock, because there is no branch
below it could reach. Every mutating branch needs a lifecycle entry -- ``issued``
needs REQUESTED, ``queued`` needs QUEUED -- so an empty history already returned
None down every path. Only the locking was load-bearing, and only in the wrong
direction.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    Score,
    ScoreAuditEntry,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_RELEASED,
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
)
from ditto.db.queries.score_retests import (
    V9_CONTRACT_RETEST_BASIS,
    activate_next_score_retest,
    score_retest_queue_positions,
)

_NOW = datetime(2026, 7, 28, 18, 3, 1, tzinfo=UTC)
_HOTKEY = "5CFtzzb4"
_SLOT = "slot-0"

# Long enough that a real lock wait is unambiguous, short enough that a
# regression fails the suite in seconds instead of hanging CI until its own
# timeout. Nothing correct here waits at all.
_NO_WAIT = 5.0


async def _seed_issued_ticket(
    session: AsyncSession, *, agent_id: object | None = None
) -> ValidatorTicket:
    """One live lease for ``_HOTKEY`` and no re-test history whatsoever."""
    agent_id = agent_id or uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"miner-{agent_id}",
                name="oracle-mind",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW - timedelta(days=1),
            )
        )
        ticket = ValidatorTicket(
            agent_id=agent_id,
            validator_hotkey=_HOTKEY,
            bench_version=7,
            slot_id=_SLOT,
            status=TicketStatus.ISSUED,
            purpose=TicketPurpose.CANONICAL_QUORUM,
            purpose_revision=1,
            issued_at=_NOW,
            deadline=_NOW + timedelta(minutes=90),
            attempt_count=1,
            manual_retry_grants=0,
            infra_retry_grants=0,
            first_reported_at=_NOW + timedelta(minutes=1),
        )
        session.add(ticket)
    return ticket


async def _seed_contract_retest(
    session: AsyncSession,
    *,
    agent_id: UUID,
    run_id: str,
    event: str,
    request_id: str,
    name: str,
) -> None:
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=f"miner-{agent_id}",
            name=name,
            sha256=f"{agent_id.int:064x}"[-64:],
            status=AgentStatus.SCORED,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=_NOW - timedelta(days=1),
        )
    )
    session.add(
        ValidatorTicket(
            agent_id=agent_id,
            validator_hotkey=_HOTKEY,
            bench_version=9,
            status=(
                TicketStatus.EXPIRED
                if event == EVENT_SCORE_RETEST_REQUESTED
                else TicketStatus.SCORED
            ),
            purpose=TicketPurpose.CANONICAL_QUORUM,
            purpose_revision=1,
            issued_at=_NOW - timedelta(hours=2),
            deadline=_NOW - timedelta(minutes=30),
            attempt_count=1,
            retry_after=_NOW - timedelta(minutes=1),
        )
    )
    session.add(
        Score(
            agent_id=agent_id,
            bench_version=9,
            validator_hotkey=_HOTKEY,
            run_id=run_id,
            seed=1,
            composite=0.5,
            tool_mean=0.5,
            memory_mean=0.5,
            median_ms=100,
            n=351,
            details={"bench_version": 9},
            generated_at=_NOW - timedelta(hours=1),
        )
    )
    await session.flush()
    await append_audit_entry(
        session,
        agent_id=agent_id,
        validator_hotkey=_HOTKEY,
        event=event,
        payload={
            "request_id": request_id,
            "basis": V9_CONTRACT_RETEST_BASIS,
            "bench_version": 9,
            "run_id": run_id,
            "replacement_deadline": (_NOW - timedelta(minutes=30)).isoformat(),
        },
        recorded_at=_NOW - timedelta(hours=1),
    )


class TestScoreRetestLockingAndPriority:
    async def test_failed_contract_replacement_requeues_and_reissues(
        self, session: AsyncSession
    ) -> None:
        agent_id = uuid4()
        request_id = str(uuid4())
        async with session.begin():
            await _seed_contract_retest(
                session,
                agent_id=agent_id,
                run_id="failed-replacement",
                event=EVENT_SCORE_RETEST_REQUESTED,
                request_id=request_id,
                name="failed replacement",
            )

            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                slot_id="slot-1",
                required_basis=V9_CONTRACT_RETEST_BASIS,
                allow_parallel_ordinary=True,
            )

            assert promoted is not None
            assert promoted.agent_id == agent_id
            assert promoted.status == TicketStatus.ISSUED
            assert promoted.slot_id == "slot-1"
            assert promoted.retry_after is None
            lifecycle = list(
                (
                    await session.scalars(
                        select(ScoreAuditEntry)
                        .where(ScoreAuditEntry.agent_id == agent_id)
                        .order_by(ScoreAuditEntry.seq.asc())
                    )
                ).all()
            )
            assert [entry.event for entry in lifecycle] == [
                EVENT_SCORE_RETEST_REQUESTED,
                EVENT_SCORE_RETEST_QUEUED,
                EVENT_SCORE_RETEST_REQUESTED,
            ]
            assert lifecycle[1].payload["automatic_requeue"] is True
            assert lifecycle[1].payload["failed_replacement_attempts"] == 1
            assert "replacement_deadline" not in lifecycle[1].payload
            assert lifecycle[2].payload["request_id"] == request_id

    async def test_failed_contract_replacement_stops_after_three_attempts(
        self, session: AsyncSession
    ) -> None:
        agent_id = uuid4()
        request_id = str(uuid4())
        async with session.begin():
            await _seed_contract_retest(
                session,
                agent_id=agent_id,
                run_id="persistent-failure",
                event=EVENT_SCORE_RETEST_REQUESTED,
                request_id=request_id,
                name="persistent failure",
            )
            for offset in (2, 1):
                await append_audit_entry(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=_HOTKEY,
                    event=EVENT_SCORE_RETEST_REQUESTED,
                    payload={
                        "request_id": request_id,
                        "basis": V9_CONTRACT_RETEST_BASIS,
                        "bench_version": 9,
                        "run_id": "persistent-failure",
                    },
                    recorded_at=_NOW - timedelta(minutes=offset),
                )

            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                required_basis=V9_CONTRACT_RETEST_BASIS,
                allow_parallel_ordinary=True,
            )

            assert promoted is None
            ticket = await session.get(ValidatorTicket, (agent_id, 9, _HOTKEY))
            assert ticket is not None
            assert ticket.status == TicketStatus.SCORED
            assert ticket.retry_after is None
            latest = await session.scalar(
                select(ScoreAuditEntry)
                .where(ScoreAuditEntry.agent_id == agent_id)
                .order_by(ScoreAuditEntry.seq.desc())
            )
            assert latest is not None
            assert latest.event == EVENT_SCORE_RETEST_RELEASED
            assert latest.payload["automatic"] is True
            assert "failed three times" in latest.payload["reason"]

    async def test_rollout_cohort_contract_repair_precedes_older_queue_work(
        self, session: AsyncSession
    ) -> None:
        older_id = uuid4()
        cohort_id = uuid4()
        rollout_id = uuid4()
        async with session.begin():
            await _seed_contract_retest(
                session,
                agent_id=older_id,
                run_id="older-noncohort",
                event=EVENT_SCORE_RETEST_QUEUED,
                request_id=str(uuid4()),
                name="older noncohort",
            )
            await _seed_contract_retest(
                session,
                agent_id=cohort_id,
                run_id="newer-cohort",
                event=EVENT_SCORE_RETEST_QUEUED,
                request_id=str(uuid4()),
                name="newer cohort",
            )
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=8,
                    desired_version=9,
                    status="collecting",
                    cohort_size=5,
                )
            )
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout_id,
                    agent_id=cohort_id,
                    position=1,
                    frozen_miner_hotkey="cohort-miner",
                    frozen_composite=0.9,
                )
            )
            await session.flush()

            positions = await score_retest_queue_positions(
                session, validator_hotkey=_HOTKEY
            )
            assert positions == {cohort_id: 1, older_id: 2}

            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                required_basis=V9_CONTRACT_RETEST_BASIS,
                allow_parallel_ordinary=True,
            )
            assert promoted is not None
            assert promoted.agent_id == cohort_id

    async def test_parallel_ordinary_mode_is_contract_only(
        self, session: AsyncSession
    ) -> None:
        with pytest.raises(
            ValueError, match="parallel ordinary work is reserved for v9 contract"
        ):
            await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda _version: True,
                allow_parallel_ordinary=True,
            )

    async def test_returns_none_without_waiting_on_a_held_ticket_row(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The validator-wide ``FOR UPDATE`` is the half that deadlocked prod.

        A concurrent transaction holding one of this validator's issued tickets
        stands in for the other side of the cycle. The score path must not reach
        for that row on its way out.
        """
        await _seed_issued_ticket(session)

        async with session_maker() as holder, holder.begin():
            held = await holder.scalar(
                select(ValidatorTicket)
                .where(ValidatorTicket.validator_hotkey == _HOTKEY)
                .with_for_update()
            )
            assert held is not None

            async with session_maker() as scorer, scorer.begin():
                promoted = await asyncio.wait_for(
                    activate_next_score_retest(
                        scorer,
                        validator_hotkey=_HOTKEY,
                        now=_NOW,
                        supports_version=lambda _version: True,
                        slot_id=_SLOT,
                    ),
                    timeout=_NO_WAIT,
                )
            assert promoted is None

    async def test_contract_retest_uses_one_free_slot_during_ordinary_work(
        self, session: AsyncSession
    ) -> None:
        """Typed contract repair skips older outliers and stays single-flight."""
        statistical_id = uuid4()
        contract_id = uuid4()
        busy_id = uuid4()
        async with session.begin():
            for agent_id, status, name in (
                (statistical_id, AgentStatus.SCORED, "statistical"),
                (contract_id, AgentStatus.EVALUATING, "contract"),
                (busy_id, AgentStatus.EVALUATING, "ordinary"),
            ):
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"miner-{agent_id}",
                        name=name,
                        sha256=f"{agent_id.int:064x}"[-64:],
                        status=status,
                        screening_policy_version=SCREENING_POLICY_VERSION,
                        created_at=_NOW - timedelta(days=1),
                    )
                )
            for agent_id, run_id in (
                (statistical_id, "statistical-run"),
                (contract_id, "contract-run"),
            ):
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=_HOTKEY,
                        bench_version=9,
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=_NOW - timedelta(hours=2),
                        deadline=_NOW - timedelta(minutes=30),
                        attempt_count=1,
                    )
                )
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=9,
                        validator_hotkey=_HOTKEY,
                        run_id=run_id,
                        seed=1,
                        composite=0.5,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=100,
                        n=1,
                        details={"bench_version": 9},
                        generated_at=_NOW - timedelta(hours=1),
                    )
                )
            session.add(
                ValidatorTicket(
                    agent_id=busy_id,
                    validator_hotkey=_HOTKEY,
                    bench_version=9,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=_NOW,
                    deadline=_NOW + timedelta(minutes=90),
                    attempt_count=1,
                )
            )
            await session.flush()
            await append_audit_entry(
                session,
                agent_id=statistical_id,
                validator_hotkey=_HOTKEY,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={
                    "request_id": str(uuid4()),
                    "bench_version": 9,
                    "run_id": "statistical-run",
                },
                recorded_at=_NOW,
            )
            await append_audit_entry(
                session,
                agent_id=contract_id,
                validator_hotkey=_HOTKEY,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={
                    "request_id": str(uuid4()),
                    "basis": "v9_contract_mismatch",
                    "bench_version": 9,
                    "run_id": "contract-run",
                },
                recorded_at=_NOW,
            )

            occupied = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                slot_id="slot-0",
                required_basis="v9_contract_mismatch",
                allow_parallel_ordinary=True,
            )
            assert occupied is None

            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                slot_id="slot-1",
                required_basis="v9_contract_mismatch",
                allow_parallel_ordinary=True,
            )

            assert promoted is not None
            assert promoted.agent_id == contract_id
            assert promoted.slot_id == "slot-1"
            statistical = await session.get(
                ValidatorTicket, (statistical_id, 9, _HOTKEY)
            )
            busy = await session.get(ValidatorTicket, (busy_id, 9, _HOTKEY))
            assert statistical is not None
            assert statistical.status == TicketStatus.SCORED
            assert busy is not None
            assert busy.status == TicketStatus.ISSUED

            second = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda version: version == 9,
                slot_id="slot-2",
                required_basis="v9_contract_mismatch",
                allow_parallel_ordinary=True,
            )
            assert second is None

    async def test_returns_none_without_waiting_on_the_advisory_lock(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The other half: ``lock_validator`` serializes the whole hotkey.

        Two score submissions for the same validator are ordinary parallel
        traffic -- that is what multi-slot capacity is for -- so one must never
        queue behind the other for a re-test queue neither of them has.
        """
        await _seed_issued_ticket(session)

        async with session_maker() as holder, holder.begin():
            await holder.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(_HOTKEY, 0)))
            )

            async with session_maker() as scorer, scorer.begin():
                promoted = await asyncio.wait_for(
                    activate_next_score_retest(
                        scorer,
                        validator_hotkey=_HOTKEY,
                        now=_NOW,
                        supports_version=lambda _version: True,
                        slot_id=_SLOT,
                    ),
                    timeout=_NO_WAIT,
                )
            assert promoted is None

    async def test_a_queued_retest_never_waits_on_a_busy_queue_lock(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A queued re-test stays queued when another transaction owns the lock.

        Waiting is unsafe here because job issuance may already own ordinary
        ticket rows the lock holder needs. Returning ``None`` is retry-safe: the
        append-only QUEUED lifecycle remains intact for the next poll.
        """
        ticket = await _seed_issued_ticket(session)
        async with session.begin():
            await append_audit_entry(
                session,
                agent_id=ticket.agent_id,
                validator_hotkey=_HOTKEY,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={"request_id": str(uuid4()), "bench_version": 7},
                recorded_at=_NOW,
            )

        async with session_maker() as holder, session_maker() as scorer:
            await holder.begin()
            await holder.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(_HOTKEY, 0)))
            )
            async with scorer.begin():
                promoted = await asyncio.wait_for(
                    activate_next_score_retest(
                        scorer,
                        validator_hotkey=_HOTKEY,
                        now=_NOW,
                        supports_version=lambda _version: True,
                        slot_id=_SLOT,
                    ),
                    timeout=_NO_WAIT,
                )
            assert promoted is None

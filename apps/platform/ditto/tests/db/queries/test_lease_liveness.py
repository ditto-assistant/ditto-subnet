"""The verdict matrix for the gate that decides whether a lease may be revoked.

Every entry here is a claim about what the platform is allowed to conclude from
one stored heartbeat. The rule the tests encode is one-directional: only a fresh,
post-issuance observation that positively reports an empty slot may end a run.
Everything else -- no row, a stale row, an unreadable capacity blob, a blob that
predates the lease -- is *unknown*, and unknown must read as running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorTicket,
)
from ditto.db.queries.audit import EVENT_SCORE_RETEST_REQUESTED, append_audit_entry
from ditto.db.queries.lease_liveness import (
    IDLE_EVIDENCE_MAX_AGE,
    LEASE_REPORTING_GRACE,
    LeaseLiveness,
    force_expire_lease,
    lease_liveness,
)
from ditto.db.queries.retry_budget import MAX_INFRA_RETRY_GRANTS
from ditto.db.queries.score_retests import activate_next_score_retest
from ditto.db.queries.tickets import (
    MAX_ATTEMPTS_PER_VERSION,
    expire_overdue_tickets,
    ticket_attempt_cap,
)

_NOW = datetime(2026, 7, 25, 13, 33, 16, tzinfo=UTC)
_HOTKEY = "5Rizzo"
_SLOT = "slot-0"
_AGENT = uuid4()


def _ticket(*, issued_at: datetime, reported: bool = True) -> ValidatorTicket:
    """A lease that has already testified once, unless *reported* says otherwise.

    Defaulting to reported keeps every case below about the question it was
    written to ask -- what one stored heartbeat proves. A lease that has never
    reported is a prior and stronger refusal that short-circuits all of them, so
    it gets its own class rather than silently making the rest vacuous.
    """
    return ValidatorTicket(
        agent_id=_AGENT,
        validator_hotkey=_HOTKEY,
        bench_version=7,
        slot_id=_SLOT,
        status=TicketStatus.ISSUED,
        issued_at=issued_at,
        deadline=issued_at + timedelta(minutes=90),
        attempt_count=1,
        manual_retry_grants=0,
        # Explicit because the column default is only applied on flush, and the
        # revocation path reads this before the row is written.
        infra_retry_grants=0,
        first_reported_at=issued_at + timedelta(minutes=1) if reported else None,
    )


def _capacity(*active: dict) -> dict:
    return {
        "configured_slots": 1,
        "healthy_slots": [_SLOT],
        "admission": "accepting",
        "active": list(active),
    }


def _active(
    *, agent_id: object = _AGENT, slot_id: str = _SLOT, deadline: datetime = _NOW
) -> dict:
    return {
        "slot_id": slot_id,
        "agent_id": str(agent_id),
        "bench_version": 7,
        "progress": {
            "stage": "running_benchmark",
            "completed": 143,
            "total": 281,
            "ticket_deadline": deadline.isoformat(),
        },
    }


async def _seed_heartbeat(
    session: AsyncSession,
    *,
    seen_at: datetime,
    protocol_version: int = 11,
    state: str = "polling",
    benchmark_capacity: dict | None = None,
    claimed_slots: list[dict] | None = None,
) -> None:
    async with session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=_HOTKEY,
                software_version="1.0.0",
                protocol_version=protocol_version,
                code_digest="ab" * 32,
                state=state,
                first_seen_at=seen_at,
                reported_at=seen_at,
                seen_at=seen_at,
                signature="cd" * 64,
                benchmark_capacity=benchmark_capacity,
                claimed_slots=claimed_slots,
            )
        )


class TestLeaseLiveness:
    async def test_no_heartbeat_row_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "heartbeat_missing"

    async def test_stale_heartbeat_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - IDLE_EVIDENCE_MAX_AGE - timedelta(seconds=1),
            benchmark_capacity=_capacity(),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "heartbeat_stale"

    async def test_observation_predating_the_lease_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session, seen_at=_NOW - timedelta(seconds=5), benchmark_capacity=_capacity()
        )
        verdict = await lease_liveness(
            session,
            # Issued moments ago: the run has not had time to announce itself.
            ticket=_ticket(issued_at=_NOW - timedelta(seconds=30)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "evidence_predates_lease"

    @pytest.mark.parametrize("capacity", [None, {"nonsense": True}])
    async def test_unreadable_capacity_reads_as_running(
        self, session: AsyncSession, capacity: dict | None
    ) -> None:
        await _seed_heartbeat(
            session, seen_at=_NOW - timedelta(seconds=5), benchmark_capacity=capacity
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "capacity_unreadable"

    async def test_slot_listed_active_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            state="running_benchmark",
            benchmark_capacity=_capacity(_active()),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "slot_active"

    async def test_same_agent_running_on_another_slot_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            state="running_benchmark",
            benchmark_capacity={
                "configured_slots": 2,
                "healthy_slots": ["slot-0", "slot-1"],
                "admission": "accepting",
                "active": [_active(slot_id="slot-1")],
            },
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "agent_active_on_another_slot"

    async def test_fresh_post_issuance_empty_capacity_is_idle(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - LEASE_REPORTING_GRACE - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is True
        assert verdict.reason == "idle_capacity_reports_slot_free"
        assert verdict.evidence["heartbeat_age_seconds"] == 5.0

    async def test_pre_v10_reporter_falls_back_to_signed_state(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session, seen_at=_NOW - timedelta(seconds=5), protocol_version=7
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is True
        assert verdict.reason == "idle_state_not_running_benchmark"

    async def test_pre_v10_running_state_reads_as_running(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            protocol_version=7,
            state="running_benchmark",
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False

    async def test_caller_reported_running_short_circuits(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session, seen_at=_NOW - timedelta(seconds=5), benchmark_capacity=_capacity()
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
            running_benchmark_reported=True,
        )
        assert verdict.idle is False
        assert verdict.reason == "running_benchmark_reported"

    async def test_revocation_refuses_a_verdict_that_is_not_idle(
        self, session: AsyncSession
    ) -> None:
        """No future call site can revoke on absence of evidence by accident."""
        ticket = _ticket(issued_at=_NOW - timedelta(hours=1))
        with pytest.raises(ValueError, match="not proven idle"):
            await force_expire_lease(
                session,
                ticket=ticket,
                now=_NOW,
                liveness=LeaseLiveness(idle=False, reason="heartbeat_stale"),
                context="issue_ticket",
            )
        assert ticket.status == TicketStatus.ISSUED


class TestRevocationDoesNotBillTheMiner:
    """ditto-platform#460's rule, enforced on the path that actually revokes.

    #460 put the compensation in the signed ``fail_job`` handler only. A
    force-expired lease cannot reach it: the revocation sets ``EXPIRED`` and
    rewrites ``deadline = now``, and ``get_open_ticket`` wants an ``ISSUED``
    ticket whose deadline matches exactly and is still future, so a late
    ``fail_job`` resolves to nothing. A validator proven idle usually never
    reports at all. So the miner was billed for every lease the platform
    destroyed, which is how a held agent reached ``attempt_count: 4,
    retry_budget_exhausted: true`` without four real failures.
    """

    _IDLE = LeaseLiveness(idle=True, reason="idle_capacity_reports_slot_free")

    async def test_force_expiry_grants_a_compensating_retry(
        self, session: AsyncSession
    ) -> None:
        ticket = _ticket(issued_at=_NOW - timedelta(hours=1))
        assert ticket.infra_retry_grants == 0
        async with session.begin():
            # Transient on purpose: the ticket's agent is not seeded, and the
            # revocation only has to mutate the object and write its audit row.
            await force_expire_lease(
                session,
                ticket=ticket,
                now=_NOW,
                liveness=self._IDLE,
                context="issue_ticket",
            )
        assert ticket.status == TicketStatus.EXPIRED
        # The cap moves, not the count: the ledger still says how many leases
        # were consumed, the grant says how many were not the miner's fault.
        assert ticket.infra_retry_grants == 1
        assert ticket.attempt_count == 1
        assert ticket_attempt_cap(ticket) == MAX_ATTEMPTS_PER_VERSION + 1

    async def test_the_grant_is_bounded(self, session: AsyncSession) -> None:
        """A persistently sick slot must not mint attempts forever."""
        ticket = _ticket(issued_at=_NOW - timedelta(hours=1))
        ticket.infra_retry_grants = MAX_INFRA_RETRY_GRANTS
        async with session.begin():
            # Transient on purpose: the ticket's agent is not seeded, and the
            # revocation only has to mutate the object and write its audit row.
            await force_expire_lease(
                session,
                ticket=ticket,
                now=_NOW,
                liveness=self._IDLE,
                context="issue_ticket",
            )
        assert ticket.infra_retry_grants == MAX_INFRA_RETRY_GRANTS
        assert ticket.status == TicketStatus.EXPIRED

    async def test_a_revoked_lease_does_not_lose_budget_across_reissue(
        self, session: AsyncSession
    ) -> None:
        """The end-to-end claim: revoke then reissue leaves the budget intact.

        This is what the held agents needed. Without the grant the reissue's
        ``attempt_count += 1`` is charged against an unchanged cap, so each
        platform revocation costs one of two genuine same-version attempts.
        """
        ticket = _ticket(issued_at=_NOW - timedelta(hours=1))
        headroom_before = ticket_attempt_cap(ticket) - ticket.attempt_count
        async with session.begin():
            # Transient on purpose: the ticket's agent is not seeded, and the
            # revocation only has to mutate the object and write its audit row.
            await force_expire_lease(
                session,
                ticket=ticket,
                now=_NOW,
                liveness=self._IDLE,
                context="issue_ticket",
            )
        # What every reissue lane does to a reused row.
        ticket.attempt_count += 1
        assert ticket_attempt_cap(ticket) - ticket.attempt_count == headroom_before


class TestScoreRetestLane:
    """The re-test lane ends a lease differently (closed unserviceable rather
    than expired) but must apply the same evidence rule before doing it."""

    async def _seed_requested_retest(
        self, session: AsyncSession, *, issued_at: datetime, reported: bool = True
    ) -> ValidatorTicket:
        async with session.begin():
            session.add(
                Agent(
                    agent_id=_AGENT,
                    miner_hotkey="miner-1",
                    name="retest-subject",
                    sha256="ab" * 32,
                    status=AgentStatus.SCORED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW - timedelta(days=1),
                )
            )
            ticket = ValidatorTicket(
                agent_id=_AGENT,
                validator_hotkey=_HOTKEY,
                bench_version=7,
                slot_id=_SLOT,
                status=TicketStatus.ISSUED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=issued_at,
                deadline=issued_at + timedelta(minutes=90),
                attempt_count=1,
                manual_retry_grants=0,
                first_reported_at=(
                    issued_at + timedelta(minutes=1) if reported else None
                ),
            )
            session.add(ticket)
            await append_audit_entry(
                session,
                agent_id=_AGENT,
                validator_hotkey=_HOTKEY,
                event=EVENT_SCORE_RETEST_REQUESTED,
                payload={"request_id": str(uuid4()), "bench_version": 7},
                recorded_at=issued_at,
            )
        return ticket

    async def test_live_retest_survives_a_dropped_benchmark_version(
        self, session: AsyncSession
    ) -> None:
        ticket = await self._seed_requested_retest(
            session, issued_at=_NOW - timedelta(minutes=19)
        )
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(minutes=4),
            benchmark_capacity=_capacity(),
        )
        async with session.begin():
            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                # The validator stopped advertising v7 -- but its heartbeat is
                # four minutes stale, so nothing proves the run stopped.
                supports_version=lambda _version: False,
                slot_id=_SLOT,
            )
        assert promoted is None
        assert ticket.status == TicketStatus.ISSUED

    async def test_proven_idle_retest_is_closed_and_audited(
        self, session: AsyncSession
    ) -> None:
        ticket = await self._seed_requested_retest(
            session, issued_at=_NOW - timedelta(minutes=19)
        )
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        async with session.begin():
            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda _version: False,
                slot_id=_SLOT,
            )
        assert promoted is None
        assert ticket.status == TicketStatus.SCORED
        async with session.begin():
            audit = (await session.scalars(select(ValidatorLeaseAudit))).all()
        assert len(audit) == 1
        assert audit[0].action == "closed_unserviceable"
        assert audit[0].context == "score_retest"

    async def test_never_reported_retest_is_not_closed(
        self, session: AsyncSession
    ) -> None:
        """The retest lane ends a lease by a different verb, under the same rule.

        It is the one revocation site that does not go through
        ``maybe_force_expire_lease``, so it is also the one that could drift.
        """
        ticket = await self._seed_requested_retest(
            session, issued_at=_NOW - timedelta(minutes=19), reported=False
        )
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        async with session.begin():
            promoted = await activate_next_score_retest(
                session,
                validator_hotkey=_HOTKEY,
                now=_NOW,
                supports_version=lambda _version: False,
                slot_id=_SLOT,
            )
        assert promoted is None
        assert ticket.status == TicketStatus.ISSUED
        async with session.begin():
            assert (await session.scalars(select(ValidatorLeaseAudit))).all() == []


class TestUnconfirmedSlotIsNotIdle:
    """A slot the ingest could not confirm must never read as proof of idleness.

    ``benchmark_capacity`` holds only the slots the platform managed to confirm
    against a live ticket. A healthy run whose lease was re-issued in place
    signs progress with the deadline it cached, so it can be evicted from that
    blob while still scoring. Before ``claimed_slots`` existed, that eviction was
    indistinguishable from "the slot is free" and the run was force-expired --
    the exact class of failure #437 was written to stop, re-entering through the
    per-slot filter.
    """

    async def test_claimed_slot_absent_from_capacity_is_not_idle(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
            claimed_slots=[{"slot_id": _SLOT, "agent_id": str(_AGENT)}],
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - LEASE_REPORTING_GRACE - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "slot_claimed_but_unconfirmed"

    async def test_claimed_agent_on_another_slot_is_not_idle(
        self, session: AsyncSession
    ) -> None:
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
            claimed_slots=[{"slot_id": "slot-3", "agent_id": str(_AGENT)}],
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - LEASE_REPORTING_GRACE - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "agent_claimed_on_another_slot"

    @pytest.mark.parametrize(
        "claimed",
        [
            None,
            [],
            [{"slot_id": "slot-2", "agent_id": str(uuid4())}],
            "not-a-list",
            [None, 7, {"slot_id": None}],
        ],
    )
    async def test_no_claim_covering_the_slot_still_reads_idle(
        self, session: AsyncSession, claimed: object
    ) -> None:
        """The claim only ever *refuses* a revocation; it must not block a real one.

        A genuinely free slot is still reclaimable, and a malformed claim is
        treated as no evidence rather than raising.
        """
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
            claimed_slots=claimed,  # type: ignore[arg-type]
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - LEASE_REPORTING_GRACE - timedelta(hours=1)),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is True
        assert verdict.reason == "idle_capacity_reports_slot_free"


class TestNeverReportedLeaseIsNotRevocable:
    """A lease that has never testified cannot be revoked, however long it waits.

    This is the failure that destroyed live v7 runs. The validator omits a leased
    slot from ``capacity.active`` until its first progress report, so a slot that
    is still pulling its image, rendering its dataset, or seeding is *absent* --
    and absent is what an idle slot looks like too. Every other safeguard is
    derived from that same list, including the ``claimed_slots`` fallback, so
    they all read false together and only the grace timer was left. Seeding alone
    may take 15 minutes against a 5-minute grace, which is why every observed
    death landed just past the window rather than inside it.
    """

    async def test_never_reported_lease_survives_an_idle_capacity_blob(
        self, session: AsyncSession
    ) -> None:
        """The exact production shape: fresh, post-grace, and reporting nothing."""
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(minutes=7), reported=False),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "lease_never_reported"
        assert verdict.evidence["lease_age_seconds"] == 420.0

    @pytest.mark.parametrize("minutes", [6, 12, 60, 89])
    async def test_no_amount_of_elapsed_time_makes_silence_evidence(
        self, session: AsyncSession, minutes: int
    ) -> None:
        """Every observed death was 5m53s-11m32s in; none of those may revoke now."""
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(minutes=minutes), reported=False),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is False
        assert verdict.reason == "lease_never_reported"

    async def test_a_lease_that_reported_once_is_still_revocable(
        self, session: AsyncSession
    ) -> None:
        """The guard must not disarm the reclaim it was built around.

        A validator that restarted keeps heartbeating and proves its own slot
        idle. Having testified once, its later silence is real evidence.
        """
        await _seed_heartbeat(
            session,
            seen_at=_NOW - timedelta(seconds=5),
            benchmark_capacity=_capacity(),
        )
        verdict = await lease_liveness(
            session,
            ticket=_ticket(issued_at=_NOW - timedelta(minutes=7), reported=True),
            validator_hotkey=_HOTKEY,
            slot_id=_SLOT,
            now=_NOW,
        )
        assert verdict.idle is True
        assert verdict.reason == "idle_capacity_reports_slot_free"

    async def test_a_crashed_validator_still_loses_its_slot_on_the_deadline(
        self, session: AsyncSession
    ) -> None:
        """The backstop that pays for the conservatism above.

        A validator that dies while seeding never reports, so the gate will now
        refuse to revoke its lease forever. That is only affordable because the
        deadline sweep is unconditional and does not consult the gate at all --
        the slot comes back at 90 minutes exactly as it always has for a
        validator that crashed before it ever polled.
        """
        issued_at = _NOW - timedelta(minutes=95)
        async with session.begin():
            session.add(
                Agent(
                    agent_id=_AGENT,
                    miner_hotkey="5Miner",
                    name="never-reported",
                    sha256="ab" * 32,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW - timedelta(days=1),
                )
            )
            session.add(_ticket(issued_at=issued_at, reported=False))

        async with session.begin():
            assert await expire_overdue_tickets(session, now=_NOW) == 1

        async with session.begin():
            ticket = await session.get(ValidatorTicket, (_AGENT, 7, _HOTKEY))
            assert ticket is not None
            assert ticket.status is TicketStatus.EXPIRED
            # Reclaimed by the deadline, not rewritten to `now` -- the signature
            # that told a real expiry apart from a force-expiry in the ledger.
            deadline = ticket.deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            assert deadline == issued_at + timedelta(minutes=90)

"""Append-only score re-tests with slot-scoped execution ownership.

The score stays canonical while an operator request waits in the audit log.
Ordinary re-tests remain single-flight. Activation-critical v9 contract repair
may use several distinct free slots on one validator; the validator advisory
lock still serializes queue promotion and preserves deterministic FIFO order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
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
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_RELEASED,
    EVENT_SCORE_RETEST_REQUESTED,
    SCORE_RETEST_EVENTS,
    append_audit_entry,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.lease_liveness import (
    LeaseLiveness,
    lease_liveness,
    record_declined_force_expiry,
    record_lease_revocation,
)

REPLACEMENT_TICKET_TTL = timedelta(minutes=90)
V9_CONTRACT_RETEST_BASIS = "v9_contract_mismatch"
MAX_AUTOMATIC_CONTRACT_RETEST_ATTEMPTS = 3
_FINALIZED_STATUSES = (AgentStatus.SCORED, AgentStatus.LIVE)


def _agent_retestable(agent: Agent | None, entry: ScoreAuditEntry) -> bool:
    if agent is None:
        return False
    if agent.status in _FINALIZED_STATUSES:
        return True
    # Contract repair is also necessary while a rollout candidate is still
    # accumulating its quorum or held for source review. Statistical outliers
    # require a finalized three-score set, but one already-accepted shadow
    # score is independently known to be non-authoritative and may be replaced
    # without waiting for the other validators or the human review to finish.
    return entry.payload.get("basis") == V9_CONTRACT_RETEST_BASIS and agent.status in {
        AgentStatus.EVALUATING,
        AgentStatus.ATH_PENDING_REVIEW,
    }


async def try_lock_validator(session: AsyncSession, validator_hotkey: str) -> bool:
    """Try to serialize ticket ownership changes for one validator.

    Job issuance can reach this queue after it has already locked ordinary
    ticket rows. Waiting here for a transaction that owns this advisory lock
    and is itself waiting for one of those rows creates a classic lock-order
    cycle. A busy queue lock is not lost work: the queued re-test remains
    append-only and the next poll retries promotion.
    """
    if session.get_bind().dialect.name != "postgresql":
        return True
    locked = await session.scalar(
        select(
            func.pg_try_advisory_xact_lock(func.hashtextextended(validator_hotkey, 0))
        )
    )
    return bool(locked)


async def latest_retest_events_for_validator(
    session: AsyncSession, *, validator_hotkey: str
) -> dict[UUID, ScoreAuditEntry]:
    """Return the latest lifecycle entry for every queued/recent agent."""
    entries = list(
        (
            await session.scalars(
                select(ScoreAuditEntry)
                .where(
                    ScoreAuditEntry.validator_hotkey == validator_hotkey,
                    ScoreAuditEntry.event.in_(SCORE_RETEST_EVENTS),
                )
                .order_by(ScoreAuditEntry.seq.asc())
            )
        ).all()
    )
    return {entry.agent_id: entry for entry in entries}


async def score_retest_queue_positions(
    session: AsyncSession, *, validator_hotkey: str
) -> dict[UUID, int]:
    latest = await latest_retest_events_for_validator(
        session, validator_hotkey=validator_hotkey
    )
    queued = await _ordered_queued_retests(
        session,
        latest=latest,
        required_basis=None,
    )
    return {entry.agent_id: index for index, entry in enumerate(queued, start=1)}


async def _close_unserviceable(
    session: AsyncSession,
    *,
    entry: ScoreAuditEntry,
    now: datetime,
    reason: str,
) -> None:
    await append_audit_entry(
        session,
        agent_id=entry.agent_id,
        validator_hotkey=entry.validator_hotkey,
        event=EVENT_SCORE_RETEST_RELEASED,
        payload={
            "request_id": entry.payload.get("request_id"),
            "retest_request_id": entry.payload.get("request_id"),
            "actor": "platform:score-retest-queue",
            "reason": reason,
            "bench_version": entry.payload.get("bench_version"),
            "preserved_run_id": entry.payload.get("run_id"),
            "automatic": True,
        },
        recorded_at=now,
    )


async def _requeue_failed_contract_retests(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    latest: dict[UUID, ScoreAuditEntry],
    now: datetime,
) -> None:
    """Return failed v9 contract replacements to their preserved queue.

    A replacement reuses the consumed score ticket. ``fail_job`` therefore
    changes that row to ``expired`` while the append-only lifecycle remains
    ``score_retest_requested``. Without a matching transition the promoter sees
    neither an issued replacement nor a queued one, so the repair disappears
    forever. Restore the accepted score's consumed-ticket state and append a
    fresh queue event. The exact request is bounded: persistent agent failures
    close visibly after three attempts and require a new operator decision.
    """
    for entry in tuple(latest.values()):
        if (
            entry.event != EVENT_SCORE_RETEST_REQUESTED
            or entry.payload.get("basis") != V9_CONTRACT_RETEST_BASIS
        ):
            continue
        bench_version = int(entry.payload.get("bench_version", -1))
        ticket = await session.get(
            ValidatorTicket,
            (entry.agent_id, bench_version, validator_hotkey),
            with_for_update=True,
        )
        if ticket is None or ticket.status != TicketStatus.EXPIRED:
            continue
        # The accepted score is still canonical while its repair is retried or
        # released. Restore the consumed-ticket shape before either transition;
        # leaving it expired would make the preserved score impossible to queue
        # again through the guarded operator endpoint.
        ticket.status = TicketStatus.SCORED
        ticket.retry_after = None
        score = await session.get(
            Score, (entry.agent_id, bench_version, validator_hotkey)
        )
        agent = await session.get(Agent, entry.agent_id)
        if (
            not _agent_retestable(agent, entry)
            or score is None
            or score.run_id != entry.payload.get("run_id")
        ):
            await _close_unserviceable(
                session,
                entry=entry,
                now=now,
                reason="accepted score changed after replacement failure",
            )
            continue

        request_id = entry.payload.get("request_id")
        if request_id is None:
            await _close_unserviceable(
                session,
                entry=entry,
                now=now,
                reason="replacement lifecycle is missing its request id",
            )
            continue
        attempts = int(
            await session.scalar(
                select(func.count(ScoreAuditEntry.seq)).where(
                    ScoreAuditEntry.agent_id == entry.agent_id,
                    ScoreAuditEntry.validator_hotkey == validator_hotkey,
                    ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
                    ScoreAuditEntry.payload["bench_version"].as_integer()
                    == bench_version,
                    ScoreAuditEntry.payload["request_id"].as_string()
                    == str(request_id),
                )
            )
            or 0
        )
        if attempts >= MAX_AUTOMATIC_CONTRACT_RETEST_ATTEMPTS:
            await _close_unserviceable(
                session,
                entry=entry,
                now=now,
                reason="replacement failed three times; operator review required",
            )
            continue

        payload = dict(entry.payload)
        payload.pop("replacement_deadline", None)
        payload["automatic_requeue"] = True
        payload["failed_replacement_attempts"] = attempts
        await append_audit_entry(
            session,
            agent_id=entry.agent_id,
            validator_hotkey=validator_hotkey,
            event=EVENT_SCORE_RETEST_QUEUED,
            payload=payload,
            recorded_at=now,
        )


async def _rollout_member_positions(
    session: AsyncSession, *, bench_version: int
) -> dict[UUID, int]:
    rollout = await session.scalar(
        select(BenchmarkRollout).where(
            BenchmarkRollout.desired_version == bench_version,
            BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")),
        )
    )
    if rollout is None:
        return {}
    rows = await session.execute(
        select(BenchmarkRolloutMember.agent_id, BenchmarkRolloutMember.position).where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id
        )
    )
    return dict(rows.tuples().all())


async def _ordered_queued_retests(
    session: AsyncSession,
    *,
    latest: dict[UUID, ScoreAuditEntry],
    required_basis: str | None,
) -> list[ScoreAuditEntry]:
    """Use one ordering contract for issuance and operator-visible positions."""
    queued = [
        entry
        for entry in latest.values()
        if entry.event == EVENT_SCORE_RETEST_QUEUED
        and (required_basis is None or entry.payload.get("basis") == required_basis)
    ]
    contract_versions = {
        int(entry.payload.get("bench_version", -1))
        for entry in queued
        if entry.payload.get("basis") == V9_CONTRACT_RETEST_BASIS
    }
    rollout_positions: dict[UUID, int] = {}
    for bench_version in contract_versions:
        rollout_positions.update(
            await _rollout_member_positions(session, bench_version=bench_version)
        )
    return sorted(
        queued,
        key=lambda entry: (
            0 if entry.agent_id in rollout_positions else 1,
            rollout_positions.get(entry.agent_id, entry.seq),
            entry.seq,
        ),
    )


async def activate_next_score_retest(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    supports_version: Callable[[int], bool],
    validator_running_benchmark: bool = False,
    slot_id: str = "slot-0",
    required_basis: str | None = None,
    allow_parallel_ordinary: bool = False,
    allow_parallel_contract_retests: bool = False,
) -> ValidatorTicket | None:
    """Resume the active re-test or promote the oldest runnable queued item.

    Must be called inside a transaction. Stale requests close append-only and
    never mutate the accepted score. By default an unrelated live assignment
    keeps all queued requests waiting. Activation-critical contract repair may
    explicitly use otherwise-free slots alongside ordinary rollout work and
    other contract repairs. Each slot still owns at most one lease, and the
    advisory lock serializes promotion so one queue entry cannot be claimed
    twice by concurrent slot polls.
    """
    if (
        allow_parallel_ordinary or allow_parallel_contract_retests
    ) and required_basis != V9_CONTRACT_RETEST_BASIS:
        raise ValueError("parallel execution is reserved for v9 contract retests")
    # Look before locking. A validator with no re-test lifecycle at all cannot
    # reach any mutating branch below -- `issued` needs a REQUESTED entry and
    # `queued` needs a QUEUED one, so an empty history returns None down every
    # path -- and this is the overwhelmingly common case: re-tests are an
    # operator action, and on prod exactly one hotkey has ever had one.
    #
    # It is not a free no-op, though, and that is the point. Taking the locks
    # first meant every score submission ended by widening its lock footprint
    # from "my one ticket" to a validator-wide `SELECT ... FOR UPDATE` over
    # every issued ticket that hotkey holds, acquired LAST -- after the
    # transaction already held the agent row and its own ticket row. The
    # issuance path guards a *narrower* key (`validator:slot`, not `validator`),
    # so the two do not exclude each other and the orders invert. Postgres broke
    # the resulting cycle the only way it can: by aborting one side, which on
    # this path is a 500 that throws away a finished 90-minute run and bills the
    # miner a `scoring_error` for it.
    #
    # This read is unlocked, so a re-test queued concurrently may be missed.
    # That is safe by construction: the queueing transaction runs its own
    # activation under the lock, and any later job request or score submission
    # picks it up. Losing a lock a caller cannot use is not a lost re-test.
    probe = await latest_retest_events_for_validator(
        session, validator_hotkey=validator_hotkey
    )
    if required_basis is not None:
        probe = {
            agent_id: entry
            for agent_id, entry in probe.items()
            if entry.payload.get("basis") == required_basis
        }
    if not probe:
        return None

    if not await try_lock_validator(session, validator_hotkey):
        return None
    # Re-read under the lock; the unlocked probe above decides only whether
    # there is anything worth locking for, never what to do with it.
    latest = await latest_retest_events_for_validator(
        session, validator_hotkey=validator_hotkey
    )
    await _requeue_failed_contract_retests(
        session,
        validator_hotkey=validator_hotkey,
        latest=latest,
        now=now,
    )
    latest = await latest_retest_events_for_validator(
        session, validator_hotkey=validator_hotkey
    )

    issued_rows = list(
        (
            await session.scalars(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.validator_hotkey == validator_hotkey,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                )
                .with_for_update()
            )
        ).all()
    )
    issued_retests = [
        ticket
        for ticket in issued_rows
        if ticket.purpose == TicketPurpose.CANONICAL_QUORUM
        and ticket.purpose_revision > 0
        if (lifecycle := latest.get(ticket.agent_id)) is not None
        and lifecycle.event == EVENT_SCORE_RETEST_REQUESTED
        and (required_basis is None or lifecycle.payload.get("basis") == required_basis)
    ]
    issued = next(
        (ticket for ticket in issued_retests if ticket.slot_id == slot_id), None
    )
    # Retests remain serialized behind every live ticket by default. A typed,
    # activation-critical caller may share the validator with ordinary work and
    # other contract repairs. Ownership stays slot-scoped: a poll resumes only
    # the repair already bound to its own slot and can never displace another
    # live lease.
    if issued_rows and issued is None and not allow_parallel_ordinary:
        return None
    if issued_retests and issued is None and not allow_parallel_contract_retests:
        return None
    if issued is None and any(ticket.slot_id == slot_id for ticket in issued_rows):
        return None
    if issued is not None:
        if issued.slot_id != slot_id:
            return None
        lifecycle = latest.get(issued.agent_id)
        if (
            lifecycle is not None
            and lifecycle.event == EVENT_SCORE_RETEST_REQUESTED
            and int(lifecycle.payload.get("bench_version", -1)) == issued.bench_version
        ):
            deadline = issued.deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if deadline > now and supports_version(issued.bench_version):
                return issued
            liveness = LeaseLiveness(idle=True, reason="lease_deadline_passed")
            if deadline > now:
                # The lease has not expired; the validator merely stopped
                # advertising this benchmark version. Closing it here ends a run
                # that may still be scoring, so it needs the same positive proof
                # of idleness the issuance lanes now require -- an absent slot in
                # a capacity blob that can silently freeze is not proof.
                liveness = await lease_liveness(
                    session,
                    ticket=issued,
                    validator_hotkey=validator_hotkey,
                    slot_id=slot_id,
                    now=now,
                    running_benchmark_reported=validator_running_benchmark,
                )
                if not liveness.idle:
                    record_declined_force_expiry(
                        ticket=issued, liveness=liveness, context="score_retest"
                    )
                    return None
            await record_lease_revocation(
                session,
                ticket=issued,
                now=now,
                liveness=liveness,
                context="score_retest",
                action="closed_unserviceable",
                requested_bench_version=issued.bench_version,
            )
            issued.status = TicketStatus.SCORED
            issued.retry_after = None
            await _close_unserviceable(
                session,
                entry=lifecycle,
                now=now,
                reason=(
                    "replacement ticket expired before completion"
                    if deadline <= now
                    else "validator no longer advertises this benchmark version"
                ),
            )
            await session.flush()
        else:
            return None

    queued = await _ordered_queued_retests(
        session,
        latest=latest,
        required_basis=required_basis,
    )
    for entry in queued:
        bench_version = int(entry.payload.get("bench_version", -1))
        # The floor comes first, and it is a different KIND of check from the
        # one below it. ``supports_version`` asks what this validator says it
        # can run; that is capability, and it happened to keep retired eras out
        # only because no modern validator advertises v6. Capability is not
        # policy: a validator that did advertise it would have re-leased a dead
        # era through this path, which is an UPDATE and so was never seen by
        # the ticket INSERT guard. The database refuses the re-lease now; this
        # skips it before it gets there.
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            await _close_unserviceable(
                session,
                entry=entry,
                now=now,
                reason=f"benchmark v{bench_version} is retired",
            )
            continue
        if not supports_version(bench_version):
            continue
        agent = await session.get(Agent, entry.agent_id)
        ticket = await session.get(
            ValidatorTicket,
            (entry.agent_id, bench_version, validator_hotkey),
            with_for_update=True,
        )
        score = await session.get(
            Score, (entry.agent_id, bench_version, validator_hotkey)
        )
        stale_reason = None
        if not _agent_retestable(agent, entry):
            stale_reason = "submission is no longer scoreable for this re-test"
        elif ticket is None or ticket.status != TicketStatus.SCORED:
            stale_reason = "accepted score ticket is no longer reusable"
        elif score is None or score.run_id != entry.payload.get("run_id"):
            stale_reason = "accepted score changed while the request was queued"
        if stale_reason is not None:
            await _close_unserviceable(
                session, entry=entry, now=now, reason=stale_reason
            )
            continue

        assert ticket is not None
        deadline = now + REPLACEMENT_TICKET_TTL
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CANONICAL_QUORUM
        ticket.purpose_revision += 1
        ticket.legacy_completion_allowed = False
        ticket.slot_id = slot_id
        ticket.issued_at = now
        ticket.deadline = deadline
        ticket.attempt_count += 1
        ticket.retry_after = None
        ticket.first_reported_at = None
        payload = dict(entry.payload)
        payload["replacement_deadline"] = deadline.isoformat()
        await append_audit_entry(
            session,
            agent_id=entry.agent_id,
            validator_hotkey=validator_hotkey,
            event=EVENT_SCORE_RETEST_REQUESTED,
            payload=payload,
            recorded_at=now,
        )
        await session.flush()
        return ticket
    await session.flush()
    return None


def retest_is_open(entry: ScoreAuditEntry | None) -> bool:
    return entry is not None and entry.event in {
        EVENT_SCORE_RETEST_QUEUED,
        EVENT_SCORE_RETEST_REQUESTED,
    }


def retest_is_active(entry: ScoreAuditEntry | None) -> bool:
    return entry is not None and entry.event == EVENT_SCORE_RETEST_REQUESTED


def retest_is_queued(entry: ScoreAuditEntry | None) -> bool:
    return entry is not None and entry.event == EVENT_SCORE_RETEST_QUEUED


__all__ = [
    "EVENT_SCORE_INVALIDATED",
    "REPLACEMENT_TICKET_TTL",
    "MAX_AUTOMATIC_CONTRACT_RETEST_ATTEMPTS",
    "V9_CONTRACT_RETEST_BASIS",
    "activate_next_score_retest",
    "try_lock_validator",
    "retest_is_active",
    "retest_is_open",
    "retest_is_queued",
    "score_retest_queue_positions",
]

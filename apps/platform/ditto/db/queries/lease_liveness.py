"""The one gate every platform-initiated validator lease revocation goes through.

A validator slot runs one benchmark at a time, so when a validator claims work
for a slot that still holds an unfinished lease the platform has to decide
between two irreversible-in-practice options: resume the old lease, or revoke it
and hand the slot something else. Revoking rewrites ``deadline = now``, which
destroys an in-flight benchmark run *and* burns one of the agent's bounded
same-version retries. A ~19-minute run and a retry are both expensive; an idle
slot held to its deadline is cheap. The asymmetry is the whole design.

**Absence of evidence is not evidence of idleness.** The revocation used to be
guarded by a single boolean derived from the last stored
:class:`~ditto.api_models.benchmark_capacity.BenchmarkCapacity` blob: "this slot
is not in ``capacity.active``, therefore nothing is running there". That blob is
a cache of the last heartbeat the platform successfully *ingested*, and it can
silently freeze while the run underneath it is perfectly healthy -- a 500 in the
heartbeat handler rolls the ingest transaction back (``seen_at`` and
``benchmark_capacity`` revert together), a 502 at the edge drops the beat, an
ingest slow enough to be retried loses the write, a deploy restart interrupts the
stream. In every one of those cases the blob keeps answering "slot free" for a
run that is still scoring. That is how three healthy v7 runs were destroyed with
no log line explaining it.

So this module inverts the burden of proof. A lease is revocable only on
**positive, fresh evidence of idleness that postdates the lease**:

0. The lease has been observed running at least once
   (``ValidatorTicket.first_reported_at`` is set). A lease that has *never*
   reported has produced no evidence in either direction, so no amount of
   elapsed time turns its silence into proof. This is the rule the grace window
   below was standing in for and could not enforce: the validator omits a leased
   slot from ``capacity.active`` entirely until its first progress report
   (``ditto-subnet`` ``worker.py``, ``slot.progress is None`` -> ``continue``),
   and every other safeguard here is derived from that one list -- including the
   ``claimed_slots`` fallback, which is projected from it. So a slot that was
   still rendering its dataset or seeding looked exactly like an idle one, and
   once the grace expired the lease was destroyed. Seeding alone is allowed 15
   minutes for v7 against a 5-minute grace, which is why every observed death
   landed just past the window rather than inside it.

1. The heartbeat row exists and its ``seen_at`` is within
   :data:`IDLE_EVIDENCE_MAX_AGE`. Missing, unreadable, or older than that is
   *unknown*, and unknown reads as running.
2. That observation is newer than ``issued_at + LEASE_REPORTING_GRACE``. A blob
   captured before (or in the first moments of) the lease cannot testify about
   it; a validator needs a beat or two to start a run and advertise the slot.
3. The evidence itself says idle: a parseable capacity blob that lists neither
   this slot nor this lease's agent as active (protocol v10+), or a signed
   ``state`` that is not ``running_benchmark`` (pre-v10 reporters, which have no
   per-slot capacity to consult).

Every other outcome -- and every parse failure, missing row, or stale sample --
returns :attr:`LeaseLiveness.idle` ``False``, meaning *assume the run is alive,
do not revoke*. The cost of that conservatism is bounded and known: the lease
still expires at its deadline via
:func:`~ditto.db.queries.tickets.expire_overdue_tickets`, so a genuinely dead
validator's slot is always reclaimed, just on the deadline rather than on the
next poll. A validator that merely restarted keeps heartbeating, so it satisfies
all three conditions within one heartbeat interval and reclaims its own slot
immediately -- which is the case the revocation was actually built for, since a
crashed validator does not poll at all and therefore never reaches this code.

Revocations are also no longer silent: :func:`force_expire_lease` writes a
:class:`~ditto.db.models.ValidatorLeaseAudit` row and a WARNING log carrying the
evidence it acted on, and both outcomes increment a Prometheus counter labelled
by reason, so the near-misses are as visible as the revocations.

One caller deliberately sits outside all of the above: an **operator eviction**
(:func:`operator_eviction_liveness`) answers a different question and therefore
does not consult this gate. It acts on protocol-16's positive
occupied-but-not-progressing report rather than on inferred absence, and it
never relaxes anything here -- the never-reported rule stays exactly as strict
for every automatic path, because pre-v16 validators still omit a quiet slot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from prometheus_client import Counter
from pydantic import ValidationError
from sqlalchemy import func, select

from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import ValidatorHeartbeat, ValidatorLeaseAudit, ValidatorTicket
from ditto.db.queries.retry_budget import grant_no_fault_retry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


# How old the idle observation may be and still count as evidence. Validators
# beat far more often than this, so a sample older than one missed beat plus
# slack is not "the slot is free", it is "the platform has not heard lately" --
# which is exactly the state that destroyed the v7 runs. Deliberately much
# tighter than ``DITTO_VALIDATOR_HEARTBEAT_MAX_AGE_SECONDS`` (300s): that gate
# decides whether a validator may be *given* work, and 300 seconds of blindness
# is fine for handing out a job but is five minutes of licence to destroy one.
IDLE_EVIDENCE_MAX_AGE = timedelta(seconds=120)

# How long after issuance a lease is unconditionally protected. The validator
# has to pull the screened image, generate the dataset, and start the harness
# before the slot shows up as active, and the first heartbeat carrying the new
# slot has to be ingested. Until an observation is newer than this, "the slot is
# not active" is indistinguishable from "the run has not announced itself yet".
LEASE_REPORTING_GRACE = timedelta(minutes=5)


# Reason codes. Everything except IDLE_* means "assume running, do not revoke".
REASON_RUNNING_REPORTED = "running_benchmark_reported"
REASON_NEVER_REPORTED = "lease_never_reported"
REASON_HEARTBEAT_MISSING = "heartbeat_missing"
REASON_HEARTBEAT_STALE = "heartbeat_stale"
REASON_EVIDENCE_PREDATES_LEASE = "evidence_predates_lease"
REASON_CAPACITY_UNREADABLE = "capacity_unreadable"
REASON_SLOT_ACTIVE = "slot_active"
REASON_SLOT_CLAIMED = "slot_claimed_but_unconfirmed"
REASON_AGENT_CLAIMED_ELSEWHERE = "agent_claimed_on_another_slot"
REASON_AGENT_ACTIVE_ELSEWHERE = "agent_active_on_another_slot"
REASON_IDLE_CAPACITY = "idle_capacity_reports_slot_free"
REASON_IDLE_STATE = "idle_state_not_running_benchmark"
# Operator-eviction verdicts. Not inferred idleness -- see
# :func:`operator_eviction_liveness`. Each names what the platform could see
# about the slot at the moment the operator acted, which is what makes the
# audit row defensible afterwards.
REASON_EVICT_OCCUPIED_NOT_PROGRESSING = "operator_evicted_occupied_not_progressing"
REASON_EVICT_OCCUPIED_PROGRESSING = "operator_evicted_occupied_progressing"
REASON_EVICT_OCCUPANCY_UNOBSERVABLE = "operator_evicted_occupancy_unobservable"

# ``ValidatorLeaseAudit.action`` values. An automatic revocation and an operator
# eviction end a lease the same way but are not the same event, and the audit
# feed must be able to filter them apart.
ACTION_FORCE_EXPIRED = "force_expired"
ACTION_OPERATOR_EVICTED = "operator_evicted"

LEASE_FORCE_EXPIRY_TOTAL = Counter(
    "ditto_validator_lease_force_expiry_total",
    "Validator leases revoked before their deadline by the platform.",
    ("context", "reason"),
)
LEASE_FORCE_EXPIRY_DECLINED_TOTAL = Counter(
    "ditto_validator_lease_force_expiry_declined_total",
    "Revocations declined because the lease was not proven idle.",
    ("context", "reason"),
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class LeaseLiveness:
    """Whether one lease is provably idle, and the evidence behind the verdict."""

    idle: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {"idle": self.idle, "reason": self.reason, **self.evidence}


def _assume_running(reason: str, **evidence: Any) -> LeaseLiveness:
    return LeaseLiveness(idle=False, reason=reason, evidence=evidence)


def _claim_covers(
    claimed_slots: Any, *, slot_id: str, agent_id: UUID
) -> tuple[bool, str | None]:
    """Read the signed occupancy claim: ``(claims this slot, agent's other slot)``.

    Deliberately total and defensive — a malformed or absent claim yields "no
    evidence", never an exception and never a licence to revoke. The claim can
    only ever REFUSE a revocation, so a garbage value costs nothing but the
    protection it would have granted.
    """
    if not isinstance(claimed_slots, list):
        return False, None
    elsewhere: str | None = None
    for entry in claimed_slots:
        if not isinstance(entry, dict):
            continue
        entry_slot = entry.get("slot_id")
        if entry_slot == slot_id:
            return True, None
        if entry.get("agent_id") == str(agent_id) and isinstance(entry_slot, str):
            # The validator moved this agent's run to a different slot. Releasing
            # this lease kills that run just the same.
            elsewhere = entry_slot
    return False, elsewhere


async def lease_liveness(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
    running_benchmark_reported: bool = False,
) -> LeaseLiveness:
    """Return whether ``ticket`` is provably idle and may be force-expired.

    Fails safe in every direction: only a fresh, post-issuance, positively idle
    observation returns ``idle=True``. Runs inside the caller's transaction and
    mutates nothing.
    """
    if running_benchmark_reported:
        # The caller already holds a signed report that this slot is running.
        return _assume_running(REASON_RUNNING_REPORTED)

    if ticket.first_reported_at is None:
        # This lease has never once been observed running, so there is no
        # evidence about it in either direction and the grace window is beside
        # the point. Absence from ``capacity.active`` is what a slot looks like
        # while it pulls its image, renders its dataset, or seeds -- the v7 seed
        # alone may take 15 minutes against a 5-minute grace -- and it is also
        # what a slot looks like when the validator omits it, which today it
        # does for every leased slot until the first progress report arrives.
        # Under that omission all three of the platform's other safeguards
        # (slot_running_benchmark, the active-slot check, and the claimed_slots
        # fallback, which is projected from the same list) read false together,
        # leaving the grace timer as the only protection a healthy run had.
        #
        # A never-reported lease is therefore not revocable at all. The cost is
        # bounded and already paid for elsewhere: the deadline still expires the
        # lease via ``expire_overdue_tickets``, so a validator that died during
        # seeding gives its slot back on the deadline exactly as a crashed one
        # always has.
        return _assume_running(
            REASON_NEVER_REPORTED,
            lease_age_seconds=round(
                (now - _as_utc(ticket.issued_at)).total_seconds(), 3
            ),
        )

    heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
    if heartbeat is None:
        return _assume_running(REASON_HEARTBEAT_MISSING)

    seen_at = _as_utc(heartbeat.seen_at)
    age = now - seen_at
    age_seconds = round(age.total_seconds(), 3)
    if age > IDLE_EVIDENCE_MAX_AGE:
        # The blob may be describing a world several minutes gone. Treating that
        # as proof of an idle slot is exactly the bug this module exists to stop.
        return _assume_running(
            REASON_HEARTBEAT_STALE,
            heartbeat_age_seconds=age_seconds,
            max_age_seconds=IDLE_EVIDENCE_MAX_AGE.total_seconds(),
        )

    issued_at = _as_utc(ticket.issued_at)
    if seen_at <= issued_at + LEASE_REPORTING_GRACE:
        return _assume_running(
            REASON_EVIDENCE_PREDATES_LEASE,
            heartbeat_age_seconds=age_seconds,
            lease_age_seconds=round((now - issued_at).total_seconds(), 3),
            grace_seconds=LEASE_REPORTING_GRACE.total_seconds(),
        )

    if heartbeat.protocol_version >= 10:
        if heartbeat.benchmark_capacity is None:
            return _assume_running(
                REASON_CAPACITY_UNREADABLE, heartbeat_age_seconds=age_seconds
            )
        try:
            capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
        except ValidationError:
            return _assume_running(
                REASON_CAPACITY_UNREADABLE, heartbeat_age_seconds=age_seconds
            )
        for slot in capacity.active:
            if slot.slot_id == slot_id:
                return _assume_running(
                    REASON_SLOT_ACTIVE, heartbeat_age_seconds=age_seconds
                )
            if slot.agent_id == ticket.agent_id:
                # The validator moved this agent's run to a different slot. The
                # lease is still doing work; releasing it kills that run.
                return _assume_running(
                    REASON_AGENT_ACTIVE_ELSEWHERE,
                    heartbeat_age_seconds=age_seconds,
                    active_slot_id=slot.slot_id,
                )
        # Absence from ``capacity.active`` is NOT by itself evidence of an idle
        # slot. The stored capacity holds only the slots the ingest could confirm
        # against a live ticket, and confirmation is an exact match on the
        # deadline the validator cached — a re-issued lease moves that deadline
        # and silently evicts a slot whose run is very much alive. Fall back to
        # the signed, unfiltered occupancy claim before concluding anything.
        claimed_slot, claimed_agent = _claim_covers(
            heartbeat.claimed_slots, slot_id=slot_id, agent_id=ticket.agent_id
        )
        if claimed_slot:
            return _assume_running(
                REASON_SLOT_CLAIMED,
                heartbeat_age_seconds=age_seconds,
                active_slot_ids=[slot.slot_id for slot in capacity.active],
            )
        if claimed_agent is not None:
            return _assume_running(
                REASON_AGENT_CLAIMED_ELSEWHERE,
                heartbeat_age_seconds=age_seconds,
                claimed_slot_id=claimed_agent,
            )
        return LeaseLiveness(
            idle=True,
            reason=REASON_IDLE_CAPACITY,
            evidence={
                "heartbeat_age_seconds": age_seconds,
                "protocol_version": heartbeat.protocol_version,
                "active_slot_ids": [slot.slot_id for slot in capacity.active],
                "admission": capacity.admission,
            },
        )

    # Pre-v10 reporters carry no per-slot capacity, so the signed whole-validator
    # state is the only idleness evidence there is. It is fresh and it postdates
    # the lease, so it is admissible.
    if heartbeat.state == "running_benchmark":
        return _assume_running(
            REASON_RUNNING_REPORTED, heartbeat_age_seconds=age_seconds
        )
    return LeaseLiveness(
        idle=True,
        reason=REASON_IDLE_STATE,
        evidence={
            "heartbeat_age_seconds": age_seconds,
            "protocol_version": heartbeat.protocol_version,
            "state": heartbeat.state,
        },
    )


def _slot_occupancy(
    heartbeat: ValidatorHeartbeat | None, *, ticket: ValidatorTicket, now: datetime
) -> tuple[str, dict[str, Any]]:
    """Classify what the platform can currently *see* about one leased slot.

    Total and defensive: an unparseable, missing, or stale heartbeat degrades to
    :data:`REASON_EVICT_OCCUPANCY_UNOBSERVABLE` rather than raising. This is a
    read of the record, never a permission check — the caller has already decided
    to act, and this only determines what the audit row gets to say about it.
    """
    if heartbeat is None:
        return REASON_EVICT_OCCUPANCY_UNOBSERVABLE, {"observation": "no heartbeat row"}
    seen_at = _as_utc(heartbeat.seen_at)
    evidence: dict[str, Any] = {
        "heartbeat_age_seconds": round((now - seen_at).total_seconds(), 3),
        "protocol_version": heartbeat.protocol_version,
    }
    if heartbeat.benchmark_capacity is None:
        return REASON_EVICT_OCCUPANCY_UNOBSERVABLE, {
            **evidence,
            "observation": "validator advertises no per-slot capacity",
        }
    try:
        capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
    except ValidationError:
        return REASON_EVICT_OCCUPANCY_UNOBSERVABLE, {
            **evidence,
            "observation": "capacity blob did not parse",
        }
    for slot in capacity.active:
        if slot.slot_id != ticket.slot_id:
            continue
        evidence["reported_agent_id"] = str(slot.agent_id)
        if slot.progress is None:
            # Protocol 16's "honest negative": the validator says it holds this
            # slot and has nothing to report on it. That is a hang, observed.
            return REASON_EVICT_OCCUPIED_NOT_PROGRESSING, evidence
        evidence["progress"] = slot.progress.model_dump(mode="json")
        return REASON_EVICT_OCCUPIED_PROGRESSING, evidence
    return REASON_EVICT_OCCUPANCY_UNOBSERVABLE, {
        **evidence,
        "observation": "slot absent from capacity.active",
        "active_slot_ids": [slot.slot_id for slot in capacity.active],
    }


async def operator_eviction_liveness(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    now: datetime,
    actor: str,
    reason: str,
    request_id: UUID,
) -> LeaseLiveness:
    """The verdict for a lease an operator has decided must end, right now.

    **This deliberately does not go through :func:`lease_liveness`.** That
    function infers idleness from telemetry and is built to refuse, and it stays
    exactly as strict as it is: no automatic revocation site reaches this
    function, and nothing here relaxes the never-reported guard. Pre-v16
    validators still omit a leased-but-quiet slot entirely, so the blanket rule
    remains the only safe automatic answer for them.

    But the operator route does not need the same answer, because it is asking a
    different question, and since ditto-subnet#274 (v0.35.0, accepted by
    ditto-platform#499) it can get **positive evidence** rather than an
    inference. A protocol-16 validator now announces a slot from the moment it is
    claimed and leaves :attr:`ActiveBenchmarkSlot.progress` null until there is
    something to say — the honest negative the gate always lacked. So
    ``occupied and not progressing`` is now an observation the platform can make,
    and it is exactly the observation a hang produces. That, not bare operator
    assertion, is what this acts on.

    :func:`_slot_occupancy` records which of the three it saw:

    * :data:`REASON_EVICT_OCCUPIED_NOT_PROGRESSING` — the strong case: the
      validator itself attests it holds the slot with nothing to report.
    * :data:`REASON_EVICT_OCCUPIED_PROGRESSING` — the slot is visibly working.
      The eviction still proceeds (the operator may be ending a run that is
      progressing but doomed), but it is logged at WARNING and the audit row says
      so, because that is the one shape a reviewer should question afterwards.
    * :data:`REASON_EVICT_OCCUPANCY_UNOBSERVABLE` — a pre-v16 reporter, a stale
      or missing heartbeat, or an unparseable blob. Recorded as unobservable
      rather than dressed up as evidence.

    The verdict never *blocks*: an operator eviction is admissible in all three
    states, because the fleet-starving case on 2026-07-27 was invisible on
    protocol 15 and refusing to act on invisibility is what left the operator
    with no move. What varies is only what the audit row can honestly claim.

    Confinement is what keeps this from re-opening the #437/#443 bug class: it is
    unreachable except from the admin eviction route, behind the admin bearer
    token, a named ``X-Admin-Actor``, an exact snapshot precondition, and a
    confirmation phrase distinct from every other operator action. Blast radius:
    one operator, one submission, one benchmark era, one audit row per lease.
    """
    heartbeat = await session.get(ValidatorHeartbeat, ticket.validator_hotkey)
    verdict, evidence = _slot_occupancy(heartbeat, ticket=ticket, now=now)
    if verdict == REASON_EVICT_OCCUPIED_PROGRESSING:
        logger.warning(
            "operator eviction is ending a slot that is still reporting progress "
            "agent=%s validator=%s slot=%s actor=%s evidence=%s",
            ticket.agent_id,
            ticket.validator_hotkey,
            ticket.slot_id,
            actor,
            evidence,
        )
    return LeaseLiveness(
        idle=True,
        reason=verdict,
        evidence={
            **evidence,
            "operator_actor": actor,
            "operator_reason": reason,
            "operator_request_id": str(request_id),
        },
    )


async def record_lease_revocation(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    now: datetime,
    liveness: LeaseLiveness,
    context: str,
    action: str,
    requested_bench_version: int | None = None,
) -> ValidatorLeaseAudit:
    """Log, count, and durably record one platform-initiated lease revocation.

    Refuses outright unless ``liveness`` carries an idle verdict, so no call site
    can reintroduce a revocation that acts on absence of evidence. Separate from
    the mutation because the lanes end a lease differently (expired here, closed
    unserviceable in the re-test lane) but must all be equally visible.
    """
    if not liveness.idle:
        raise ValueError(
            f"refusing to revoke a lease that was not proven idle: {liveness.reason}"
        )
    evidence = {
        **liveness.as_payload(),
        "context": context,
        "slot_id": ticket.slot_id,
        "ticket_bench_version": ticket.bench_version,
        "requested_bench_version": requested_bench_version,
        "purpose": str(ticket.purpose),
        "issued_at": _as_utc(ticket.issued_at).isoformat(),
        "original_deadline": _as_utc(ticket.deadline).isoformat(),
        "lease_age_seconds": round(
            (now - _as_utc(ticket.issued_at)).total_seconds(), 3
        ),
        "attempt_count": ticket.attempt_count,
    }
    evidence["action"] = action
    logger.warning(
        "revoking validator lease action=%s agent=%s validator=%s slot=%s "
        "bench_version=%s lease_age_s=%s reason=%s evidence=%s",
        action,
        ticket.agent_id,
        ticket.validator_hotkey,
        ticket.slot_id,
        ticket.bench_version,
        evidence["lease_age_seconds"],
        liveness.reason,
        evidence,
    )
    LEASE_FORCE_EXPIRY_TOTAL.labels(context=context, reason=liveness.reason).inc()
    audit = ValidatorLeaseAudit(
        audit_id=uuid4(),
        agent_id=ticket.agent_id,
        validator_hotkey=ticket.validator_hotkey,
        slot_id=ticket.slot_id,
        bench_version=ticket.bench_version,
        action=action,
        reason=liveness.reason,
        context=context,
        evidence=evidence,
        recorded_at=now,
    )
    session.add(audit)
    return audit


async def force_expire_lease(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    now: datetime,
    liveness: LeaseLiveness,
    context: str,
    action: str = ACTION_FORCE_EXPIRED,
    requested_bench_version: int | None = None,
    compensate: bool = True,
) -> ValidatorLeaseAudit:
    """Expire a lease proven idle, leaving a log line and an audit row behind.

    Compensates the miner on the way out. ditto-platform#460 settled the rule --
    "do not bill a miner for a lease the platform itself revoked" -- but put the
    grant only in the signed ``fail_job`` handler, which this path cannot reach:
    the revocation sets ``status = EXPIRED`` and rewrites ``deadline = now``, and
    ``get_open_ticket`` requires an ``ISSUED`` ticket whose deadline matches
    exactly and is still in the future, so a late ``fail_job`` for this lease
    resolves to nothing and the compensation never fires. A validator that was
    proven idle usually never reports at all, so in practice it never fired.

    The miner was therefore billed an attempt for every lease the platform
    destroyed -- which is how held agents reached ``attempt_count: 4,
    retry_budget_exhausted: true`` without four real failures.

    ``compensate=False`` suppresses that grant, and **only the operator eviction
    route passes it.** The grant exists to offset the attempt the *coming
    reissue* will charge; an eviction is precisely the decision that there will
    be no reissue in this era, so there is no attempt to offset. Granting anyway
    would raise the agent's cap on the way out and re-arm the exact amplifier
    behind the 2026-07-27 incident: ditto-subnet#279 established that those
    leases were not silent but *misclassified* -- all twelve expired ``mnemo*``
    tickets carry the ``fail_job(reason="infrastructure")`` signature
    (``retry_after - deadline`` of exactly +2min/+30min, the
    :func:`~ditto.db.queries.retry_budget.infra_retry_backoff` base and cap) --
    and ``infrastructure`` is the no-fault class, so every hang minted a grant,
    raised the cap and re-leased. That is how ``mnemox-v55`` reached nine
    attempts against a base budget of two with zero scores. An eviction must not
    hand the artifact it just evicted another attempt.

    ``action`` names the *kind* of revocation for the audit feed and defaults to
    the automatic one. The eviction route passes :data:`ACTION_OPERATOR_EVICTED`
    so a deliberate human eviction is never read back as an inferred idle
    verdict. Returns the audit row it wrote, so a caller can hand the operator a
    durable id to cite afterwards.
    """
    audit = await record_lease_revocation(
        session,
        ticket=ticket,
        now=now,
        liveness=liveness,
        context=context,
        action=action,
        requested_bench_version=requested_bench_version,
    )
    # Before the status change, so the grant is part of the same transaction the
    # audit row records. Bounded, so a persistently sick slot cannot mint
    # attempts forever; the audit row is the per-grant justification.
    exhausted = compensate and not grant_no_fault_retry(ticket)
    ticket.status = TicketStatus.EXPIRED
    ticket.deadline = now
    ticket.retry_after = now
    if exhausted:
        logger.warning(
            "platform revoked a lease whose no-fault retry budget is already "
            "exhausted; this revocation bills the miner agent=%s validator=%s "
            "slot=%s grants=%s",
            ticket.agent_id,
            ticket.validator_hotkey,
            ticket.slot_id,
            ticket.infra_retry_grants,
        )
    await session.flush()
    return audit


def record_declined_force_expiry(
    *,
    ticket: ValidatorTicket,
    liveness: LeaseLiveness,
    context: str,
) -> None:
    """Log + count a revocation the liveness gate refused.

    Deliberately not an audit row: a declined revocation is the *safe* outcome
    and repeats on every poll of a busy slot, so it belongs in metrics and logs
    rather than in an append-only table.
    """
    LEASE_FORCE_EXPIRY_DECLINED_TOTAL.labels(
        context=context, reason=liveness.reason
    ).inc()
    logger.info(
        "declining to force-expire live validator lease agent=%s validator=%s "
        "slot=%s bench_version=%s reason=%s evidence=%s",
        ticket.agent_id,
        ticket.validator_hotkey,
        ticket.slot_id,
        ticket.bench_version,
        liveness.reason,
        liveness.as_payload(),
    )


async def maybe_force_expire_lease(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
    context: str,
    running_benchmark_reported: bool = False,
    requested_bench_version: int | None = None,
) -> bool:
    """Force-expire ``ticket`` iff it is provably idle. Returns whether it did.

    The single entry point every revocation site uses, so the evidence rule, the
    log line, the audit row, and the metrics cannot drift apart between them.
    """
    liveness = await lease_liveness(
        session,
        ticket=ticket,
        validator_hotkey=validator_hotkey,
        slot_id=slot_id,
        now=now,
        running_benchmark_reported=running_benchmark_reported,
    )
    if not liveness.idle:
        record_declined_force_expiry(ticket=ticket, liveness=liveness, context=context)
        return False
    await force_expire_lease(
        session,
        ticket=ticket,
        now=now,
        liveness=liveness,
        context=context,
        requested_bench_version=requested_bench_version,
    )
    return True


__all__ = [
    "ACTION_FORCE_EXPIRED",
    "ACTION_OPERATOR_EVICTED",
    "IDLE_EVIDENCE_MAX_AGE",
    "LEASE_FORCE_EXPIRY_DECLINED_TOTAL",
    "LEASE_FORCE_EXPIRY_TOTAL",
    "LEASE_REPORTING_GRACE",
    "REASON_EVICT_OCCUPANCY_UNOBSERVABLE",
    "REASON_EVICT_OCCUPIED_NOT_PROGRESSING",
    "REASON_EVICT_OCCUPIED_PROGRESSING",
    "LeaseLiveness",
    "force_expire_lease",
    "lease_liveness",
    "maybe_force_expire_lease",
    "operator_eviction_liveness",
    "record_declined_force_expiry",
    "record_lease_revocation",
]


def _filter_lease_revocations(
    stmt: Select[Any],
    *,
    agent_id: UUID | None,
    validator_hotkey: str | None,
    actions: Sequence[str] | None,
    contexts: Sequence[str] | None,
    since: datetime | None,
) -> Select[Any]:
    """Apply the operator's filters. Shared so the page and its count agree."""
    if agent_id is not None:
        stmt = stmt.where(ValidatorLeaseAudit.agent_id == agent_id)
    if validator_hotkey is not None:
        stmt = stmt.where(ValidatorLeaseAudit.validator_hotkey == validator_hotkey)
    if actions:
        stmt = stmt.where(ValidatorLeaseAudit.action.in_(list(actions)))
    if contexts:
        stmt = stmt.where(ValidatorLeaseAudit.context.in_(list(contexts)))
    if since is not None:
        stmt = stmt.where(ValidatorLeaseAudit.recorded_at >= since)
    return stmt


async def count_lease_revocations(
    session: AsyncSession,
    *,
    agent_id: UUID | None = None,
    validator_hotkey: str | None = None,
    actions: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    since: datetime | None = None,
) -> int:
    """How many revocations match, ignoring paging."""
    stmt = select(func.count()).select_from(ValidatorLeaseAudit)
    return int(
        (
            await session.execute(
                _filter_lease_revocations(
                    stmt,
                    agent_id=agent_id,
                    validator_hotkey=validator_hotkey,
                    actions=actions,
                    contexts=contexts,
                    since=since,
                )
            )
        ).scalar_one()
    )


async def list_lease_revocations(
    session: AsyncSession,
    *,
    agent_id: UUID | None = None,
    validator_hotkey: str | None = None,
    actions: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ValidatorLeaseAudit]:
    """Read the revocation ledger, newest first.

    ``audit_id`` breaks ties because ``recorded_at`` is the caller's ``now``:
    two lanes revoking in one sweep share it exactly, and an unstable sort would
    silently drop or repeat a row across pages.
    """
    stmt = _filter_lease_revocations(
        select(ValidatorLeaseAudit),
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        actions=actions,
        contexts=contexts,
        since=since,
    )
    stmt = stmt.order_by(
        ValidatorLeaseAudit.recorded_at.desc(), ValidatorLeaseAudit.audit_id.desc()
    )
    return list(await session.scalars(stmt.limit(limit).offset(offset)))

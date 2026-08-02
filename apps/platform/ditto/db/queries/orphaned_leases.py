"""What a validator is still executing after the platform took its lease away.

An operator eviction (ditto-platform#515, via
:func:`~ditto.db.queries.lease_liveness.operator_eviction_liveness`) releases
the **platform's** half of a lease immediately: the
ticket goes ``EXPIRED``, the slot stops counting against the fleet's capacity,
and the submission leaves the queue. It does not, and cannot, stop the
validator. The benchmark container on the far side keeps running until it exits
on its own, and when it finally tries to submit, the score is refused with a
clean 409 and never reaches the ledger. That refusal is deliberate and stays
exactly as it is.

The consequence is a window -- minutes to over an hour -- in which a host is
burning a full benchmark's worth of CPU on a run that cannot produce a score,
and every surface the platform has says the slot is free. On 2026-07-27 nine
bench-7 submissions were evicted in two minutes, revoking five live leases
across three validators, and the fleet view rendered every one of those slots
``Idle`` while the same rows carried a "Running benchmark" badge and a running
container count. An operator reading that page sees a fleet with headroom it
does not have, and the obvious next move -- raise the slot cap -- makes it
worse.

This module derives that missing state. It is a pure read: nothing here
cancels, revokes, or re-leases anything.

**The derivation, and why each input is the authoritative one**

The eviction ledger (:class:`~ditto.db.models.ValidatorLeaseAudit` rows with
``action = operator_evicted``) says which (validator, slot, agent) triples lost
a lease, when, and what deadline the lease would otherwise have run to. That is
the "was evicted" half, and it is exact.

The "still running" half comes from
:attr:`~ditto.db.models.ValidatorHeartbeat.claimed_slots`, **not** from
``benchmark_capacity``. This is the whole trick, and getting it backwards
produces a view that is confidently wrong. The stored ``benchmark_capacity`` is
filtered at ingest to the slots the ledger could confirm against a *live*
ticket (``_validated_heartbeat_work`` in ``endpoints/validator.py``), and an
evicted lease is by definition no longer live -- so an orphaned slot is
guaranteed to be missing from ``benchmark_capacity.active`` no matter what the
validator said. ``claimed_slots`` is the same signed occupancy claim captured
*before* that filter, kept for precisely this reason, and it is where an
orphaned run remains visible.

**Protocol 15 is why there are three states and not two**

ditto-subnet#274 (v0.35.0, heartbeat protocol 16) made a validator announce a
slot from the moment it is claimed, leaving ``progress`` null until there is
something to report -- the "honest negative" that ditto-platform#499 accepts.
For such a reporter, absence of a slot from the occupancy claim is real
evidence: the slot was released.

Through protocol 15 a validator **omitted** a claimed-but-quiet slot entirely.
A slot pulling an image, rendering a dataset, seeding, or sitting between
progress reports looked exactly like a free one. For those validators absence
proves nothing, and the honest answer is that the platform does not know. The
live fleet is mixed today (two reporters on protocol 16, one on 15), so both
answers are needed at once.

Hence:

``still_running``   the validator's own signed claim still lists this slot,
                    held by the evicted agent. Positive evidence, admissible on
                    any protocol that advertises capacity at all.
``released``        either the slot is gone from a protocol-16 claim, or some
                    *other* agent now holds it (a slot runs one benchmark at a
                    time, so that ends the evicted run on any protocol). The
                    orphan is over; the slot really is free.
``indeterminate``   everything else -- a protocol-15 reporter that has simply
                    stopped mentioning the slot, a missing or stale heartbeat,
                    a validator advertising no per-slot occupancy at all.

There is no fourth branch that guesses. The rule this module exists to enforce
is that the fleet view must never render a confident ``Idle`` the data does not
support, and the fix for that must not introduce a confident "still running"
the data does not support either. Absence of evidence on a pre-v16 reporter is
``indeterminate``, full stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select

from ditto.db.models import Agent, ValidatorHeartbeat, ValidatorLeaseAudit
from ditto.db.queries.lease_liveness import (
    ACTION_OPERATOR_EVICTED,
    IDLE_EVIDENCE_MAX_AGE,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


# The first heartbeat protocol whose capacity snapshot reports a claimed slot
# before it has any progress to report (ditto-subnet#274, v0.35.0; accepted by
# ditto-platform#499). At and above this, absence of a slot from the occupancy
# claim is evidence the slot was released. Below it, absence is only silence:
# a pre-16 validator omits a leased slot until its first progress report, so it
# cannot distinguish "the container exited" from "the container is seeding".
OCCUPANCY_REPORTING_PROTOCOL_VERSION = 16

# How far back the eviction ledger is read. Comfortably longer than the longest
# benchmark lease (90 minutes for the deep contract) so a run evicted at the
# start of its lease is still tracked when it finally exits, and short enough
# that the scan stays trivial on the ``recorded_at`` index.
EVICTION_LOOKBACK = timedelta(hours=6)

# How long past the lease's original deadline an *unproven* orphan keeps being
# reported. Once the deadline the validator itself cached has passed, its own
# run timeout has fired, so continuing to say "this might still be running"
# would be asserting something the clock contradicts. Positive evidence is not
# subject to this bound: a slot the validator still actively claims is reported
# for as long as it claims it, however late that is, because that is a real
# stuck container and hiding it is the bug this module exists to fix.
INDETERMINATE_HORIZON_GRACE = timedelta(minutes=10)

# Horizon for an audit row whose evidence carries no readable original deadline.
# Only reachable if an eviction row is malformed; bounded rather than infinite.
FALLBACK_RUN_HORIZON = timedelta(hours=2)


OrphanedLeaseState = Literal["still_running", "released", "indeterminate"]

# Why the state above was concluded. Every one of these names an observation,
# never an assumption.
ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT = "validator_still_claims_slot"
ORPHAN_REASON_SLOT_CLAIMED_BY_ANOTHER_AGENT = "slot_claimed_by_another_agent"
ORPHAN_REASON_SLOT_NO_LONGER_CLAIMED = "validator_no_longer_claims_slot"
ORPHAN_REASON_PRE_V16_OMITS_QUIET_SLOT = "pre_v16_reporter_omits_a_quiet_slot"
ORPHAN_REASON_NO_OCCUPANCY_REPORT = "validator_advertises_no_slot_occupancy"
ORPHAN_REASON_HEARTBEAT_MISSING = "heartbeat_missing"
ORPHAN_REASON_HEARTBEAT_STALE = "heartbeat_stale"
ORPHAN_REASON_EVIDENCE_PREDATES_EVICTION = "evidence_predates_eviction"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OrphanedLease:
    """One evicted lease, and what the platform can see about it right now."""

    audit_id: UUID
    validator_hotkey: str
    slot_id: str
    agent_id: UUID
    agent_name: str | None
    bench_version: int
    state: OrphanedLeaseState
    reason: str
    evicted_at: datetime
    orphaned_for_seconds: float
    original_deadline: datetime | None
    protocol_version: int | None
    """The reporter's protocol at the observation, or ``None`` with no heartbeat.

    Carried through to the surface so an ``indeterminate`` row can say *why* it
    is indeterminate rather than leaving an operator to guess whether the
    platform is confused or the validator is simply too old to answer.
    """


def _original_deadline(evidence: object) -> datetime | None:
    """The deadline the evicted lease would have run to, if the row records one.

    ``evidence`` is deliberately untyped in the audit table, so this is total:
    a missing, non-dict, or unparseable value degrades to ``None`` and the
    caller falls back to a bounded horizon.
    """
    if not isinstance(evidence, dict):
        return None
    raw = evidence.get("original_deadline")
    if not isinstance(raw, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _claim_state(
    heartbeat: ValidatorHeartbeat | None,
    *,
    slot_id: str,
    agent_id: UUID,
    evicted_at: datetime,
    now: datetime,
) -> tuple[OrphanedLeaseState, str]:
    """Classify one evicted slot from the validator's latest signed claim.

    Total and defensive in the same posture as
    :func:`~ditto.db.queries.lease_liveness._claim_covers`: a malformed or
    absent claim yields *unknown*, never an exception and never a confident
    answer in either direction.
    """
    if heartbeat is None:
        return "indeterminate", ORPHAN_REASON_HEARTBEAT_MISSING
    seen_at = _as_utc(heartbeat.seen_at)
    if now - seen_at > IDLE_EVIDENCE_MAX_AGE:
        # The same freshness bar the revocation gate uses. A sample from several
        # minutes ago describes a world that may already be gone; it is not
        # proof the container exited and it is not proof it is still there.
        return "indeterminate", ORPHAN_REASON_HEARTBEAT_STALE
    if seen_at <= evicted_at:
        # A snapshot taken before the eviction cannot testify about what the
        # validator did after it. Reading it either way would be reading the
        # wrong moment.
        return "indeterminate", ORPHAN_REASON_EVIDENCE_PREDATES_EVICTION
    claimed = heartbeat.claimed_slots
    if not isinstance(claimed, list):
        # No per-slot occupancy advertised at all (a pre-v10 reporter, or a
        # heartbeat whose work payload could not be validated). Nothing to read.
        return "indeterminate", ORPHAN_REASON_NO_OCCUPANCY_REPORT
    for entry in claimed:
        if not isinstance(entry, dict) or entry.get("slot_id") != slot_id:
            continue
        if entry.get("agent_id") == str(agent_id):
            return "still_running", ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT
        # A slot runs one benchmark at a time, so another agent holding it is
        # positive evidence the evicted run is over -- admissible on any
        # protocol, because this is presence, not absence.
        return "released", ORPHAN_REASON_SLOT_CLAIMED_BY_ANOTHER_AGENT
    if heartbeat.protocol_version >= OCCUPANCY_REPORTING_PROTOCOL_VERSION:
        return "released", ORPHAN_REASON_SLOT_NO_LONGER_CLAIMED
    return "indeterminate", ORPHAN_REASON_PRE_V16_OMITS_QUIET_SLOT


def _within_horizon(
    state: OrphanedLeaseState,
    *,
    evicted_at: datetime,
    original_deadline: datetime | None,
    now: datetime,
) -> bool:
    """Whether an orphan is still worth reporting. See the module docstring."""
    if state != "indeterminate":
        return True
    horizon = (
        original_deadline + INDETERMINATE_HORIZON_GRACE
        if original_deadline is not None
        else evicted_at + FALLBACK_RUN_HORIZON
    )
    return now <= horizon


async def _agent_names(
    session: AsyncSession, *, agent_ids: Collection[UUID]
) -> dict[UUID, str]:
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(Agent.agent_id, Agent.name).where(Agent.agent_id.in_(list(agent_ids)))
    )
    return dict(rows.all())  # type: ignore[arg-type]


async def list_orphaned_leases(
    session: AsyncSession,
    *,
    now: datetime,
    live_slots: Iterable[tuple[str, str]],
    lookback: timedelta = EVICTION_LOOKBACK,
) -> list[OrphanedLease]:
    """Every evicted lease whose validator may still be executing it.

    ``live_slots`` is the set of ``(validator_hotkey, slot_id)`` pairs that hold
    a live lease right now. A slot the platform has since re-leased is dropped:
    whatever the old container is doing, the slot's row already renders real
    work, so it is no longer the false-idle the operator was misled by. That is
    a deliberate degrade, not a claim the old container stopped -- the platform
    has no way to see two runs on one slot, and inventing one here would be the
    same over-claiming this module refuses everywhere else.

    Returns all three states. Callers that render a fleet surface should drop
    ``released``, which by definition describes a slot that is genuinely idle
    and should keep rendering as idle; it is returned so the classification is
    inspectable and testable rather than silently swallowed.

    Costs nothing in the ordinary case: with no eviction in ``lookback`` the
    first query returns no rows and the function returns without touching the
    heartbeat or agent tables.
    """
    rows = list(
        await session.scalars(
            select(ValidatorLeaseAudit)
            .where(
                ValidatorLeaseAudit.action == ACTION_OPERATOR_EVICTED,
                ValidatorLeaseAudit.recorded_at >= now - lookback,
            )
            .order_by(
                ValidatorLeaseAudit.recorded_at.desc(),
                ValidatorLeaseAudit.audit_id.desc(),
            )
        )
    )
    if not rows:
        return []
    live = set(live_slots)
    # Newest eviction wins per slot: an older one on the same slot has already
    # been superseded, and reporting both would double-count one host.
    newest: dict[tuple[str, str], ValidatorLeaseAudit] = {}
    for row in rows:
        newest.setdefault((row.validator_hotkey, row.slot_id), row)
    pending = {key: row for key, row in newest.items() if key not in live}
    if not pending:
        return []
    heartbeats = {
        heartbeat.validator_hotkey: heartbeat
        for heartbeat in await session.scalars(
            select(ValidatorHeartbeat).where(
                ValidatorHeartbeat.validator_hotkey.in_(
                    sorted({hotkey for hotkey, _ in pending})
                )
            )
        )
    }
    names = await _agent_names(
        session, agent_ids={row.agent_id for row in pending.values()}
    )
    orphans: list[OrphanedLease] = []
    for (hotkey, slot_id), row in pending.items():
        evicted_at = _as_utc(row.recorded_at)
        heartbeat = heartbeats.get(hotkey)
        state, reason = _claim_state(
            heartbeat,
            slot_id=slot_id,
            agent_id=row.agent_id,
            evicted_at=evicted_at,
            now=now,
        )
        deadline = _original_deadline(row.evidence)
        if not _within_horizon(
            state, evicted_at=evicted_at, original_deadline=deadline, now=now
        ):
            continue
        orphans.append(
            OrphanedLease(
                audit_id=row.audit_id,
                validator_hotkey=hotkey,
                slot_id=slot_id,
                agent_id=row.agent_id,
                agent_name=names.get(row.agent_id),
                bench_version=row.bench_version,
                state=state,
                reason=reason,
                evicted_at=evicted_at,
                orphaned_for_seconds=round((now - evicted_at).total_seconds(), 3),
                original_deadline=deadline,
                protocol_version=(
                    heartbeat.protocol_version if heartbeat is not None else None
                ),
            )
        )
    orphans.sort(key=lambda orphan: (orphan.validator_hotkey, orphan.slot_id))
    return orphans


__all__ = [
    "EVICTION_LOOKBACK",
    "FALLBACK_RUN_HORIZON",
    "INDETERMINATE_HORIZON_GRACE",
    "OCCUPANCY_REPORTING_PROTOCOL_VERSION",
    "ORPHAN_REASON_EVIDENCE_PREDATES_EVICTION",
    "ORPHAN_REASON_HEARTBEAT_MISSING",
    "ORPHAN_REASON_HEARTBEAT_STALE",
    "ORPHAN_REASON_NO_OCCUPANCY_REPORT",
    "ORPHAN_REASON_PRE_V16_OMITS_QUIET_SLOT",
    "ORPHAN_REASON_SLOT_CLAIMED_BY_ANOTHER_AGENT",
    "ORPHAN_REASON_SLOT_NO_LONGER_CLAIMED",
    "ORPHAN_REASON_VALIDATOR_CLAIMS_SLOT",
    "OrphanedLease",
    "OrphanedLeaseState",
    "list_orphaned_leases",
]

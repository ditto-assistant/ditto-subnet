"""Guarded operator admission of one stranded previous-era submission.

:mod:`ditto.db.queries.benchmark_era_stranded` lists the scored previous-era
work an activated benchmark era never admits. This module is the operator
remedy for one exact row of that list, after activation.

**What admission writes.** One :class:`~ditto.db.models.BenchmarkRolloutMember`
row appended to the *activated* rollout after its frozen cohort, the agent's
active-version :class:`~ditto.db.models.BenchmarkDataset` pin, and a
``stranded_admitted`` rollout audit event carrying the actor, reason, exact
artifact SHA and the previous-era evidence it was admitted on -- all in one
transaction under the rollout row lock.

**Why a rollout member.** A stranded submission is ``scored`` (or ``live``) on
the previous bench. The ordinary queue only leases ``evaluating`` work, and
moving it back to ``evaluating`` would rewrite its previous-era status history.
The activated era already has a lane for exactly this shape:
:func:`~ditto.db.queries.benchmark_rollout.issue_rollout_topup_ticket` finishes
any member of the activated rollout still below the active-version quorum,
straight out of ``scored``/``live``, under the distinct-validator, retry-budget,
withdrawal and retirement rails. Membership is also an existing admission
disjunct of
:func:`~ditto.db.queries.benchmark_admission.benchmark_admission_predicate`,
and ``agent_scoring_readiness`` already recognises the activated tail. So the
admitted agent becomes admitted and leasable without a new lane, and its
previous-era scores and status history are untouched.

Every query that treats member rows as the *inherited activation cohort*
(``_validate_frozen_members``, ``rollout_cohort_complete``,
``_rollout_rescore_cohort``, ``issue_rollout_ticket``) runs only on an open
rollout, and the activation gates read positions at or below the frozen
``cohort_size``/``priority_cohort_target``. An admitted member is appended past
both and only ever on an ``activated`` row, so it can never re-gate an
activation. ``cohort_size`` is deliberately left at the frozen value; the audit
event, not the member count, is the record of an operator admission.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import func, select

from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    Score,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import (
    activated_rollout_for_version,
    agent_is_admitted,
)
from ditto.db.queries.benchmark_era_stranded import (
    StrandedEraAgent,
    stranded_previous_era_agents,
)
from ditto.db.queries.benchmark_rollout import (
    DatasetPin,
    RolloutConflictError,
    active_bench_version,
    append_rollout_audit,
    open_rollout,
)
from ditto.db.queries.scores import SCORING_QUORUM

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

EVENT_STRANDED_ADMITTED = "stranded_admitted"
ORIGIN_STRANDED_ADMISSION = "operator_stranded_admission"


class StrandedAgentNotFoundError(LookupError):
    """The named submission does not exist."""


def stranded_admission_confirmation(agent_id: UUID, bench_version: int) -> str:
    """The exact phrase an operator types to admit one stranded submission."""
    return f"ADMIT STRANDED {agent_id} TO BENCHMARK V{bench_version}"


@dataclass(frozen=True)
class StrandedAdmissionTarget:
    """One guarded stranded row, resolved against the live era."""

    rollout: BenchmarkRollout
    agent: Agent
    stranded: StrandedEraAgent


def _screened_image_complete(agent: Agent) -> bool:
    return all(
        getattr(agent, field) is not None
        for field in (
            "screened_image_sha256",
            "screened_image_size_bytes",
            "screened_image_id",
            "screened_image_ref",
            "screened_image_upload_id",
            "screened_image_verified_at",
        )
    )


async def resolve_stranded_admission(
    session: AsyncSession,
    *,
    agent_id: UUID,
    expected_active_version: int,
    expected_sha256: str,
    expected_previous_score_count: int,
) -> StrandedAdmissionTarget:
    """Check every admission guard and return the exact row to admit.

    Raises :class:`RolloutConflictError` naming the first guard that fails, or
    :class:`StrandedAgentNotFoundError` for an unknown agent. Reads only.
    """
    active = await active_bench_version(session)
    if active != expected_active_version:
        raise RolloutConflictError(
            "active benchmark changed: expected "
            f"v{expected_active_version}, found v{active}"
        )
    if await open_rollout(session) is not None:
        raise RolloutConflictError(
            "a benchmark rollout is open; stranded admission only applies to an "
            "activated era with no rollout in flight"
        )
    rollout = await activated_rollout_for_version(session, bench_version=active)
    if rollout is None or rollout.from_version >= active:
        raise RolloutConflictError(
            f"no activated v{active} rollout with a previous era to admit from"
        )
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise StrandedAgentNotFoundError(f"agent {agent_id} not found")
    if agent.sha256.lower() != expected_sha256.lower():
        raise RolloutConflictError(
            f"artifact SHA changed: expected {expected_sha256.lower()}, "
            f"found {agent.sha256.lower()}"
        )
    report = await stranded_previous_era_agents(session, agent_id=agent_id)
    if report.rollout is None or report.rollout.rollout_id != rollout.rollout_id:
        raise RolloutConflictError(
            "the stranded read resolved a different admission rollout"
        )
    if not report.items:
        raise RolloutConflictError(
            f"agent {agent_id} is not a stranded v{rollout.from_version} "
            f"submission: it is already admitted to v{active}, already holds a "
            f"v{active} score, is not scored/live, or has no "
            f"v{rollout.from_version} scores"
        )
    stranded = report.items[0]
    if stranded.previous_score_count != expected_previous_score_count:
        raise RolloutConflictError(
            f"v{rollout.from_version} score count changed: expected "
            f"{expected_previous_score_count}, found {stranded.previous_score_count}"
        )
    if stranded.queue_withdrawn:
        raise RolloutConflictError(
            f"agent {agent_id} is withdrawn from the v{active} validator queue; "
            "reinstate it before admitting"
        )
    contract = benchmark_contract(active)
    if agent.screening_policy_version < contract.minimum_screening_policy_version:
        raise RolloutConflictError(
            f"screening policy v{agent.screening_policy_version} is below the "
            f"v{active} contract minimum v{contract.minimum_screening_policy_version}"
        )
    if contract.requires_screened_image and not _screened_image_complete(agent):
        raise RolloutConflictError(
            f"agent {agent_id} has no complete screened image; v{active} could "
            "never lease it"
        )
    return StrandedAdmissionTarget(rollout=rollout, agent=agent, stranded=stranded)


async def lock_stranded_admission(
    session: AsyncSession, *, rollout_id: UUID, agent_id: UUID
) -> None:
    """Serialize one admission with concurrent issuance and queue withdrawal.

    Takes the rollout row lock and then the agent row lock, in the order every
    other issuer takes them (rollout -> agent). Queue withdrawal, eviction and
    reinstatement all lock the agent row first, so re-resolving the guards
    after this call sees any withdrawal that committed after the first read,
    and none can commit between that re-check and the append.
    """
    await session.execute(
        select(BenchmarkRollout.rollout_id)
        .where(BenchmarkRollout.rollout_id == rollout_id)
        .with_for_update()
    )
    await session.execute(
        select(Agent.agent_id).where(Agent.agent_id == agent_id).with_for_update()
    )


async def append_stranded_admission(
    session: AsyncSession,
    *,
    target: StrandedAdmissionTarget,
    dataset: DatasetPin,
    seed_source: str,
    actor: str,
    reason: str,
    now: datetime,
) -> BenchmarkRolloutMember:
    """Admit ``target`` onto its activated rollout. One agent per call.

    The caller must have just resolved ``target`` in this transaction. Takes
    the rollout row lock, appends the member past every existing position, pins
    the active-version dataset (or proves the existing pin identical) and writes
    the audited ``stranded_admitted`` event.
    """
    rollout = await session.get(
        BenchmarkRollout, target.rollout.rollout_id, with_for_update=True
    )
    if rollout is None or rollout.status != "activated":
        raise RolloutConflictError("the admission rollout is no longer activated")
    agent = target.agent
    if (
        await session.get(BenchmarkRolloutMember, (rollout.rollout_id, agent.agent_id))
        is not None
    ):
        raise RolloutConflictError(f"agent {agent.agent_id} is already admitted")
    position = (
        int(
            await session.scalar(
                select(
                    func.coalesce(func.max(BenchmarkRolloutMember.position), 0)
                ).where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            )
        )
        + 1
    )
    previous_median = target.stranded.previous_median_composite
    member = BenchmarkRolloutMember(
        rollout_id=rollout.rollout_id,
        agent_id=agent.agent_id,
        position=position,
        frozen_miner_hotkey=agent.miner_hotkey,
        frozen_composite=previous_median if previous_median is not None else 0.0,
    )
    session.add(member)
    existing_dataset = await session.get(
        BenchmarkDataset, (agent.agent_id, rollout.desired_version)
    )
    if existing_dataset is None:
        session.add(
            BenchmarkDataset(
                agent_id=agent.agent_id,
                bench_version=rollout.desired_version,
                seed=dataset.seed,
                sha256=dataset.sha256,
                run_size=dataset.run_size,
                seed_block=dataset.seed_block,
                seed_block_hash=dataset.seed_block_hash,
                created_at=now,
            )
        )
    elif (
        existing_dataset.seed,
        existing_dataset.sha256,
        existing_dataset.run_size,
        existing_dataset.seed_block,
        existing_dataset.seed_block_hash,
    ) != (
        dataset.seed,
        dataset.sha256,
        dataset.run_size,
        dataset.seed_block,
        dataset.seed_block_hash,
    ):
        raise RolloutConflictError(
            "existing benchmark dataset does not match the admission pin"
        )
    await append_rollout_audit(
        session,
        rollout,
        EVENT_STRANDED_ADMITTED,
        {
            "origin": ORIGIN_STRANDED_ADMISSION,
            "actor": actor,
            "reason": reason,
            "agent_id": str(agent.agent_id),
            "agent_sha256": agent.sha256.lower(),
            "miner_hotkey": agent.miner_hotkey,
            "owner_key": target.stranded.owner_key,
            "agent_status": target.stranded.status,
            "position": position,
            "frozen_cohort_size": rollout.cohort_size,
            "previous_bench_version": rollout.from_version,
            "previous_score_count": target.stranded.previous_score_count,
            "previous_median_composite": previous_median,
            "cohort_sibling_agent_id": (
                str(target.stranded.cohort_sibling_agent_id)
                if target.stranded.cohort_sibling_agent_id is not None
                else None
            ),
            "bench_version": rollout.desired_version,
            "dataset_seed": dataset.seed,
            "dataset_sha256": dataset.sha256,
            "dataset_run_size": dataset.run_size,
            "seed_source": seed_source,
        },
        now=now,
    )
    await session.flush()
    return member


TicketState = Literal[
    "awaiting_first_lease", "leased", "awaiting_next_lease", "quorum_complete"
]


@dataclass(frozen=True)
class StrandedAdmissionTicket:
    validator_hotkey: str
    slot_id: str
    status: str
    purpose: str
    issued_at: datetime
    deadline: datetime
    attempt_count: int


@dataclass(frozen=True)
class StrandedAdmissionEffect:
    """What one admission did: admission state and active-era lease progress."""

    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    status: str
    admitted_at: datetime
    actor: str | None
    reason: str | None
    position: int | None
    agent_sha256: str | None
    previous_bench_version: int | None
    previous_score_count: int | None
    previous_median_composite: float | None
    dataset_sha256: str | None
    is_member: bool
    admitted: bool
    active_score_count: int
    active_median_composite: float | None
    first_lease_at: datetime | None
    live_lease_count: int
    ticket_state: TicketState
    tickets: list[StrandedAdmissionTicket]


@dataclass(frozen=True)
class StrandedAdmissionsReport:
    active_bench_version: int
    rollout: BenchmarkRollout | None
    items: list[StrandedAdmissionEffect]


async def stranded_admissions(
    session: AsyncSession,
    *,
    now: datetime,
    agent_id: UUID | None = None,
) -> StrandedAdmissionsReport:
    """Every audited stranded admission on the active era, with its effect.

    Read-only. ``admitted`` is the live admission predicate (it turns false if
    the agent is later withdrawn); ``ticket_state`` summarises active-version
    lease progress toward quorum.
    """
    active = await active_bench_version(session)
    rollout = await activated_rollout_for_version(session, bench_version=active)
    if rollout is None:
        return StrandedAdmissionsReport(
            active_bench_version=active, rollout=None, items=[]
        )
    events = list(
        await session.scalars(
            select(BenchmarkRolloutAudit)
            .where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == EVENT_STRANDED_ADMITTED,
            )
            .order_by(BenchmarkRolloutAudit.recorded_at, BenchmarkRolloutAudit.audit_id)
        )
    )
    payloads: dict[UUID, tuple[datetime, dict[str, Any]]] = {}
    for audit in events:
        try:
            event_agent = UUID(str(audit.payload.get("agent_id")))
        except ValueError:
            continue
        if agent_id is not None and event_agent != agent_id:
            continue
        payloads.setdefault(event_agent, (audit.recorded_at, audit.payload))
    if not payloads:
        return StrandedAdmissionsReport(
            active_bench_version=active, rollout=rollout, items=[]
        )
    ids = list(payloads)
    agents = {
        agent.agent_id: agent
        for agent in await session.scalars(select(Agent).where(Agent.agent_id.in_(ids)))
    }
    members = set(
        await session.scalars(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.agent_id.in_(ids),
            )
        )
    )
    composites: dict[UUID, list[float]] = {}
    for score_agent, composite in (
        await session.execute(
            select(Score.agent_id, Score.composite).where(
                Score.agent_id.in_(ids), Score.bench_version == active
            )
        )
    ).all():
        composites.setdefault(score_agent, []).append(float(composite))
    tickets: dict[UUID, list[ValidatorTicket]] = {}
    for ticket in await session.scalars(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id.in_(ids),
            ValidatorTicket.bench_version == active,
        )
        .order_by(ValidatorTicket.issued_at, ValidatorTicket.validator_hotkey)
    ):
        tickets.setdefault(ticket.agent_id, []).append(ticket)

    items: list[StrandedAdmissionEffect] = []
    for admitted_id, (admitted_at, payload) in payloads.items():
        agent = agents.get(admitted_id)
        if agent is None:
            continue
        agent_tickets = tickets.get(admitted_id, [])
        scores = composites.get(admitted_id, [])
        live = [
            ticket
            for ticket in agent_tickets
            if ticket.status == TicketStatus.ISSUED and ticket.deadline > now
        ]
        state: TicketState
        if len(scores) >= SCORING_QUORUM:
            state = "quorum_complete"
        elif live:
            state = "leased"
        elif agent_tickets:
            state = "awaiting_next_lease"
        else:
            state = "awaiting_first_lease"
        items.append(
            StrandedAdmissionEffect(
                agent_id=admitted_id,
                agent_name=agent.name,
                miner_hotkey=agent.miner_hotkey,
                status=agent.status.value,
                admitted_at=admitted_at,
                actor=payload.get("actor"),
                reason=payload.get("reason"),
                position=payload.get("position"),
                agent_sha256=payload.get("agent_sha256"),
                previous_bench_version=payload.get("previous_bench_version"),
                previous_score_count=payload.get("previous_score_count"),
                previous_median_composite=payload.get("previous_median_composite"),
                dataset_sha256=payload.get("dataset_sha256"),
                is_member=admitted_id in members,
                admitted=await agent_is_admitted(
                    session, bench_version=active, agent_id=admitted_id
                ),
                active_score_count=len(scores),
                active_median_composite=(statistics.median(scores) if scores else None),
                first_lease_at=(
                    min(ticket.issued_at for ticket in agent_tickets)
                    if agent_tickets
                    else None
                ),
                live_lease_count=len(live),
                ticket_state=state,
                tickets=[
                    StrandedAdmissionTicket(
                        validator_hotkey=ticket.validator_hotkey,
                        slot_id=ticket.slot_id,
                        status=TicketStatus(ticket.status).value,
                        purpose=str(ticket.purpose),
                        issued_at=ticket.issued_at,
                        deadline=ticket.deadline,
                        attempt_count=ticket.attempt_count,
                    )
                    for ticket in agent_tickets
                ],
            )
        )
    return StrandedAdmissionsReport(
        active_bench_version=active, rollout=rollout, items=items
    )


__all__ = [
    "EVENT_STRANDED_ADMITTED",
    "ORIGIN_STRANDED_ADMISSION",
    "StrandedAdmissionEffect",
    "StrandedAdmissionTarget",
    "StrandedAdmissionTicket",
    "StrandedAdmissionsReport",
    "StrandedAgentNotFoundError",
    "append_stranded_admission",
    "lock_stranded_admission",
    "resolve_stranded_admission",
    "stranded_admission_confirmation",
    "stranded_admissions",
]

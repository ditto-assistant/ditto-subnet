"""Public submission status shared by miner lists, and the ``era_changed`` label.

Activity, the agent summary and the submission pipeline derive their status
from the active era's admission predicate. The miner profile,
``/miner/me/submissions`` and the miner MCP list used to read submissions
straight off ``agents.status``. That left over a hundred previous-era
submissions reading "evaluating" on miner pages forever while activity said
they were not queued (SN-462), and the stranded v17-finalized rows read
``scored`` with nothing saying they no longer compete (SN-485).

This module is where those lists close out such rows, using the same
predicates as the queue. It only reads. It never changes queue, lease,
admission, retry, withdrawal or score state, so a row returns to its earlier
label as soon as an operator or a rollout admits it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.era_change import PublicEraChange
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import Agent, BenchmarkRollout, ValidatorTicket
from ditto.db.queries.benchmark_admission import (
    admission_rollout_for_active_version,
    benchmark_admission_predicate,
    previous_era_scored_agent_ids,
    validator_queue_admission_predicate,
)
from ditto.db.queries.queue_removal import removed_agent_ids
from ditto.db.queries.retirement import retired_agent_ids
from ditto.db.queries.terminal_failure import terminal_failure_codes

PUBLIC_ACTIVITY_STATUSES = frozenset(
    {
        "waiting_screening",
        "screening_failed",
        "screening",
        "waiting_validator",
        "evaluating",
        "below_score_floor",
        "not_queued",
        "era_changed",
        "retired",
        "scoring_failed",
        "under_review",
        "rejected",
        "scored",
        "live",
    }
)

_INTERNAL_TO_PUBLIC_STATUS = {
    "uploaded": "waiting_screening",
    "screening_failed": "screening_failed",
    "screening": "screening",
    "screening_passed": "waiting_validator",
    "evaluating": "evaluating",
    "ath_pending_review": "under_review",
    "quarantined": "under_review",
    "banned": "rejected",
}

_WAITING_STATUSES = (AgentStatus.SCREENING_PASSED, AgentStatus.EVALUATING)
_FINALIZED_STATUSES = (AgentStatus.SCORED, AgentStatus.LIVE)


def live_lease_predicate(*, bench_version: int, now: datetime) -> ColumnElement[bool]:
    """SQL form of :func:`holds_live_lease` for one ``ValidatorTicket`` row.

    The same "assignment" rule as ``list_active_validator_assignments``, which
    feeds the activity projection: an issued lease whose deadline has not
    passed. An expired ``issued`` row is not a validator holding the work.
    """
    return (
        (ValidatorTicket.status == TicketStatus.ISSUED)
        & (ValidatorTicket.bench_version == bench_version)
        & (ValidatorTicket.deadline > now)
    )


def holds_live_lease(
    tickets: Iterable[ValidatorTicket], *, bench_version: int, now: datetime
) -> bool:
    """Whether a validator currently holds an unexpired lease on ``bench_version``.

    While one does, the row keeps its waiting label as the lease drains, so
    neither the miner projection nor the operator stuck list calls it
    ``era_changed``.
    """
    return any(
        ticket.status == TicketStatus.ISSUED
        and ticket.bench_version == bench_version
        and _aware(ticket.deadline) > now
        for ticket in tickets
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def public_agent_status(raw: str) -> str:
    """Map a stored status onto the public vocabulary, without queue state."""
    mapped = _INTERNAL_TO_PUBLIC_STATUS.get(raw, raw)
    if mapped in PUBLIC_ACTIVITY_STATUSES:
        return mapped
    return "under_review"


def era_change(
    *,
    bench_version: int,
    rollout: BenchmarkRollout | None,
    previous_era_scored: bool,
) -> PublicEraChange:
    """The reason and next step on one ``era_changed`` row.

    ``previous_era_scored`` marks a row that finalized on the admission
    rollout's source version. Its scores there stay visible, so the payload
    names that version.
    """
    return PublicEraChange(
        reason="not_admitted_to_active_bench",
        bench_version=bench_version,
        scored_bench_version=(
            rollout.from_version
            if previous_era_scored and rollout is not None
            else None
        ),
        next_step="resubmit",
    )


def era_change_label(change: PublicEraChange) -> str:
    """The dashboard chip, quoted by operator surfaces."""
    return f"Era changed · resubmit for v{change.bench_version}"


def era_change_blocking_reason(change: PublicEraChange) -> str:
    """Operator blocking reason for a row the era boundary keeps out.

    Backroom quotes the label miners see, so an operator reads the same
    cause, not a quorum or retry message that can never apply to the row.
    """
    version = change.bench_version
    return (
        f"historical submission is not admitted to the v{version} validator "
        f"queue ({change.reason}); miners see '{era_change_label(change)}', "
        f"next step: resubmit to compete on v{version}"
    )


def closed_era_blocking_reason(*, era_version: int, active_version: int) -> str:
    """Operator blocking reason for retry state in an era nobody serves."""
    return (
        f"bench v{era_version} is no longer served (validators score only "
        f"v{active_version}), so this v{era_version} retry state will not run"
    )


@dataclass(frozen=True)
class ClosedSubmissionStatus:
    """The public status of a stored row that will not be scored now."""

    status: Literal["retired", "scoring_failed", "not_queued", "era_changed"]
    era_change: PublicEraChange | None = None


async def closed_submission_statuses(
    session: AsyncSession,
    *,
    agents: Sequence[Agent],
    active_version: int,
    now: datetime | None = None,
) -> dict[UUID, ClosedSubmissionStatus]:
    """Close out a miner list's rows with the activity projection's predicates.

    Rows missing from the result keep their stored-status mapping. Waiting rows
    are checked in the activity order: retired, then a terminal close-out in
    the row's own scoring era, then an in-force withdrawal (``not_queued``), then the
    era boundary (``era_changed``). A row that a validator holds on an
    active-version lease is skipped. Finalized rows become ``era_changed`` only
    when :func:`previous_era_scored_predicate` names them. Every read is bounded
    by the given agents, and a list with nothing to close costs only the
    rollout read.
    """
    now = now or datetime.now(UTC)
    waiting = [agent.agent_id for agent in agents if agent.status in _WAITING_STATUSES]
    finalized = [
        agent.agent_id for agent in agents if agent.status in _FINALIZED_STATUSES
    ]
    if not waiting and not finalized:
        return {}
    rollout = await admission_rollout_for_active_version(
        session, bench_version=active_version
    )
    result: dict[UUID, ClosedSubmissionStatus] = {}
    for agent_id in await previous_era_scored_agent_ids(
        session, agent_ids=finalized, bench_version=active_version, rollout=rollout
    ):
        result[agent_id] = ClosedSubmissionStatus(
            status="era_changed",
            era_change=era_change(
                bench_version=active_version, rollout=rollout, previous_era_scored=True
            ),
        )
    if not waiting:
        return result
    retired = await retired_agent_ids(session, agent_ids=set(waiting))
    for agent_id in retired:
        result[agent_id] = ClosedSubmissionStatus(status="retired")
    # Each row's own resolved scoring era, not the active one: a terminal
    # close-out keeps reading ``scoring_failed`` on a miner's list after the
    # board moves on, which is the more specific answer for that artifact.
    terminal = await terminal_failure_codes(
        session,
        agent_ids=[agent_id for agent_id in waiting if agent_id not in retired],
    )
    for agent_id in terminal:
        result[agent_id] = ClosedSubmissionStatus(status="scoring_failed")
    open_rows = [agent_id for agent_id in waiting if agent_id not in result]
    if not open_rows:
        return result
    admitted_statement = select(Agent.agent_id).where(
        Agent.agent_id.in_(open_rows),
        validator_queue_admission_predicate(bench_version=active_version),
    )
    if rollout is not None:
        admitted_statement = admitted_statement.where(
            benchmark_admission_predicate(rollout=rollout, bench_version=active_version)
        )
    admitted = set(await session.scalars(admitted_statement))
    refused = [agent_id for agent_id in open_rows if agent_id not in admitted]
    if not refused:
        return result
    # A validator holding a live active-era lease keeps the row in its waiting
    # label while it drains, as in the activity projection. An ``issued`` row
    # past its deadline is not a held lease.
    held = set(
        await session.scalars(
            select(ValidatorTicket.agent_id).where(
                ValidatorTicket.agent_id.in_(refused),
                live_lease_predicate(bench_version=active_version, now=now),
            )
        )
    )
    closed = [agent_id for agent_id in refused if agent_id not in held]
    withdrawn = await removed_agent_ids(
        session, agent_ids=closed, bench_version=active_version
    )
    for agent_id in closed:
        result[agent_id] = (
            ClosedSubmissionStatus(status="not_queued")
            if agent_id in withdrawn
            else ClosedSubmissionStatus(
                status="era_changed",
                era_change=era_change(
                    bench_version=active_version,
                    rollout=rollout,
                    previous_era_scored=False,
                ),
            )
        )
    return result


async def public_statuses(
    session: AsyncSession,
    *,
    agents: Sequence[Agent],
    active_version: int,
    now: datetime | None = None,
) -> dict[UUID, str]:
    """The status each agent's miner sees, for operator reads (SN-483).

    The miner projection's close-out (``retired``, ``scoring_failed``,
    ``not_queued``, ``era_changed``) when :func:`closed_submission_statuses`
    names the row, otherwise the stored status in the public vocabulary.
    Backroom shows this beside the stored ``agent_status`` so an operator
    never reads a closed row as live work. Bounded by the given agents.
    """
    closed = await closed_submission_statuses(
        session, agents=agents, active_version=active_version, now=now
    )
    return {
        agent.agent_id: (
            closed[agent.agent_id].status
            if agent.agent_id in closed
            else public_agent_status(agent.status.value)
        )
        for agent in agents
    }


__all__ = [
    "PUBLIC_ACTIVITY_STATUSES",
    "ClosedSubmissionStatus",
    "closed_era_blocking_reason",
    "closed_submission_statuses",
    "era_change",
    "era_change_blocking_reason",
    "era_change_label",
    "holds_live_lease",
    "live_lease_predicate",
    "public_agent_status",
    "public_statuses",
]

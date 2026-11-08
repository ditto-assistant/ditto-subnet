"""Close out a submission whose own failures made scoring quorum unreachable.

A submission whose remaining quorum slots all died on the same named
agent-attributable failure (``inference_allowance_exhausted`` and the rest of
:data:`~ditto.db.queries.retry_state.AGENT_ATTRIBUTABLE_FAILURE_DETAILS`) will
never score: re-leasing the same image repeats the same failure. Before this
module that verdict existed only at read time, so the row stayed ``evaluating``
indefinitely. Every fresh validator could still take a first assignment and
burn a full run on it, and public surfaces kept saying "waiting for
validators".

The platform now records the verdict as an ordinary
:class:`~ditto.db.models.ValidatorQueueWithdrawal` carrying a
``terminal_failure_code``. Every queue predicate already reads that table, so
the allocator stops issuing tickets, and the code is what lets public surfaces
say ``scoring_failed`` instead of a neutral ``not_queued``. Like every
withdrawal it is scoped to one benchmark version, preserves the payment,
artifact, scores and ticket history, and an operator can reinstate it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    Score,
    SubmissionRetirement,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.queue_removal import load_queue_removal, removal_in_force
from ditto.db.queries.retry_state import (
    AGENT_ATTRIBUTABLE_FAILURE_DETAIL_VALUES,
    dominant_agent_failure_detail,
    is_agent_attributable_exhaustion,
    resolve_bench_version,
    withdrawal_gate,
)
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.tickets import scoring_retry_ticket

logger = logging.getLogger(__name__)

TERMINAL_ARTIFACT_FAILURE_ACTOR = "platform:terminal-artifact-failure"
"""``actor`` on a withdrawal the platform wrote without an operator."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def ticket_snapshot_wire(ticket: ValidatorTicket) -> dict[str, object]:
    """The audit snapshot of one ticket stored on a withdrawal row."""
    return {
        "validator_hotkey": ticket.validator_hotkey,
        "status": ticket.status.value,
        "issued_at": _aware(ticket.issued_at).isoformat(timespec="microseconds"),
        "deadline": _aware(ticket.deadline).isoformat(timespec="microseconds"),
        "bench_version": ticket.bench_version,
        "attempt_count": ticket.attempt_count,
        "manual_retry_grants": ticket.manual_retry_grants,
        "infra_retry_grants": ticket.infra_retry_grants,
        "retry_after": (
            _aware(ticket.retry_after).isoformat(timespec="microseconds")
            if ticket.retry_after is not None
            else None
        ),
    }


def terminal_failure_code(
    *, scores: list[Score], tickets: list[ValidatorTicket]
) -> str | None:
    """The agreed agent-attributable code that makes quorum unreachable.

    ``None`` unless every remaining quorum slot is exhausted on one named
    agent-attributable code. The same fail-closed predicates the operator
    triage uses: a live lease, a timeout, a missing or stale detail, or mixed
    causes all return ``None``.
    """
    if not is_agent_attributable_exhaustion(scores=scores, tickets=tickets):
        return None
    return dominant_agent_failure_detail(scores=scores, tickets=tickets)


def _close_out_digest(
    *,
    agent: Agent,
    bench_version: int,
    code: str,
    snapshot: list[dict[str, object]],
) -> str:
    """A SHA-256 of the evidence the close-out acted on.

    ``expected_snapshot`` is a 64-hex digest on every operator-written row and
    Backroom validates it as one, so the platform's own row uses the same
    shape rather than a readable tag that would fail that parse.
    """
    payload = {
        "actor": TERMINAL_ARTIFACT_FAILURE_ACTOR,
        "agent_id": str(agent.agent_id),
        "artifact_sha256": agent.sha256,
        "bench_version": bench_version,
        "terminal_failure_code": code,
        "tickets": snapshot,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def withdraw_terminal_artifact_failure(
    session: AsyncSession,
    *,
    agent_id: UUID,
    now: datetime,
) -> ValidatorQueueWithdrawal | None:
    """Withdraw ``agent_id`` from its era if its own failures ended scoring.

    Call inside a transaction. Locks the agent row and then its tickets, the
    same order as the operator withdraw route, so the two serialize instead of
    deadlocking, and a second concurrent caller sees the first one's row.
    Returns the new withdrawal, or ``None`` when nothing was written.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None:
        return None
    all_tickets = list(
        (
            await session.scalars(
                select(ValidatorTicket)
                .where(ValidatorTicket.agent_id == agent_id, scoring_retry_ticket())
                .order_by(
                    ValidatorTicket.deadline.asc(),
                    ValidatorTicket.validator_hotkey.asc(),
                )
                .with_for_update()
            )
        ).all()
    )
    all_scores = list(
        (await session.scalars(select(Score).where(Score.agent_id == agent_id))).all()
    )
    bench_version = resolve_bench_version(
        all_tickets=all_tickets,
        all_scores=all_scores,
        canonical_version=await active_bench_version(session),
    )
    scores = [score for score in all_scores if score.bench_version == bench_version]
    tickets = [
        ticket for ticket in all_tickets if ticket.bench_version == bench_version
    ]

    code = terminal_failure_code(scores=scores, tickets=tickets)
    if code is None:
        return None
    allowed, _reason = withdrawal_gate(
        agent=agent, scores=scores, tickets=tickets, bench_version=bench_version
    )
    if not allowed:
        return None
    if (
        await load_queue_removal(
            session, agent_id=agent_id, bench_version=bench_version, for_update=True
        )
        is not None
    ):
        return None
    if (
        await session.scalar(
            select(SubmissionRetirement.agent_id).where(
                SubmissionRetirement.agent_id == agent_id,
                SubmissionRetirement.bench_version == bench_version,
            )
        )
        is not None
    ):
        return None

    snapshot = [ticket_snapshot_wire(ticket) for ticket in tickets]
    withdrawal = ValidatorQueueWithdrawal(
        withdrawal_id=uuid.uuid4(),
        agent_id=agent_id,
        bench_version=bench_version,
        actor=TERMINAL_ARTIFACT_FAILURE_ACTOR,
        reason=(
            "every remaining quorum slot failed on the agent's own "
            f"{code}; this artifact cannot finish scoring"
        ),
        expected_snapshot=_close_out_digest(
            agent=agent, bench_version=bench_version, code=code, snapshot=snapshot
        ),
        score_count=len(scores),
        ticket_snapshot=snapshot,
        terminal_failure_code=code,
        created_at=now,
    )
    session.add(withdrawal)
    await session.flush()
    logger.warning(
        "withdrew terminal artifact failure agent=%s bench_version=%s code=%s "
        "score_count=%s",
        agent_id,
        bench_version,
        code,
        len(scores),
    )
    return withdrawal


async def terminal_failure_codes(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[UUID, str]:
    """In-force terminal withdrawal codes for ``agent_ids`` in one era."""
    ids = list(agent_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                ValidatorQueueWithdrawal.agent_id,
                ValidatorQueueWithdrawal.terminal_failure_code,
            ).where(
                ValidatorQueueWithdrawal.agent_id.in_(ids),
                ValidatorQueueWithdrawal.bench_version == bench_version,
                ValidatorQueueWithdrawal.terminal_failure_code.is_not(None),
                removal_in_force(),
            )
        )
    ).all()
    return {agent_id: str(code) for agent_id, code in rows}


async def terminal_close_out_candidates(
    session: AsyncSession, *, limit: int
) -> list[UUID]:
    """Evaluating agents that may be owed a close-out, oldest first.

    Narrow enough that rows which cannot qualify do not pile up at the head of
    every sweep and starve the ones that can: per (agent, benchmark version),
    at least as many expired scoring tickets whose *current* detail is an
    allowlisted agent code as scores are still owed, no live ticket, and no
    in-force withdrawal for that same version. Each candidate is then decided
    by :func:`withdraw_terminal_artifact_failure`, which applies the full
    fail-closed predicates under row locks.
    """
    live = (
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.agent_id == Agent.agent_id,
            ValidatorTicket.status == TicketStatus.ISSUED,
        )
        .correlate(Agent)
        .exists()
    )
    withdrawn = (
        select(ValidatorQueueWithdrawal.agent_id)
        .where(
            ValidatorQueueWithdrawal.agent_id == Agent.agent_id,
            ValidatorQueueWithdrawal.bench_version == ValidatorTicket.bench_version,
            removal_in_force(),
        )
        .correlate(Agent, ValidatorTicket)
        .exists()
    )
    scored = (
        select(func.count(Score.validator_hotkey))
        .where(
            Score.agent_id == Agent.agent_id,
            Score.bench_version == ValidatorTicket.bench_version,
        )
        .correlate(Agent, ValidatorTicket)
        .scalar_subquery()
    )
    statement = (
        select(Agent.agent_id, Agent.created_at)
        .join(ValidatorTicket, ValidatorTicket.agent_id == Agent.agent_id)
        .where(
            Agent.status == AgentStatus.EVALUATING,
            ValidatorTicket.status == TicketStatus.EXPIRED,
            ValidatorTicket.failure_detail.in_(
                sorted(AGENT_ATTRIBUTABLE_FAILURE_DETAIL_VALUES)
            ),
            # A detail older than the lease belongs to a superseded attempt.
            ValidatorTicket.failed_at >= ValidatorTicket.issued_at,
            scoring_retry_ticket(),
            ~live,
            ~withdrawn,
        )
        .group_by(Agent.agent_id, Agent.created_at, ValidatorTicket.bench_version)
        .having(
            func.count(func.distinct(ValidatorTicket.validator_hotkey))
            >= SCORING_QUORUM - scored
        )
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(limit)
    )
    seen: dict[UUID, None] = {}
    for agent_id, _created_at in (await session.execute(statement)).all():
        seen.setdefault(agent_id, None)
    return list(seen)


async def sweep_terminal_artifact_failures(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Close out every candidate that qualifies; returns how many were written.

    The safety net under the ``/validator/job/fail`` hook: that hook runs after
    the report commits and only logs a failure, so a close-out it dropped, and
    any row stranded before close-outs existed, is applied here. Each agent
    gets its own transaction so one bad row cannot block the rest.
    """
    at = now or datetime.now(UTC)
    async with session_maker() as session:
        candidates = await terminal_close_out_candidates(session, limit=limit)
    written = 0
    for agent_id in candidates:
        try:
            async with session_maker() as session, session.begin():
                if await withdraw_terminal_artifact_failure(
                    session, agent_id=agent_id, now=at
                ):
                    written += 1
        except Exception:
            logger.exception(
                "terminal artifact failure sweep failed agent=%s", agent_id
            )
    return written

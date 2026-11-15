"""A per-agent cap on free within-era re-leases (SN-485 section 3).

Within one benchmark era an agent gets at most one lease per validator
(``MAX_ATTEMPTS_PER_VERSION`` in :mod:`ditto.db.queries.tickets`), and the
terminal-failure close-out withdraws it only when every remaining quorum slot
failed on *one named agent-attributable* code. Every other failure (a lease
that ran to its deadline, an unnamed or mixed cause, a first
``harness_killed_137``) is treated as possibly ours, so the queue keeps handing
the agent to the next free validator. That is the only free path a miner can
drive with no constant bound: one fee could buy a full run on every validator
in the fleet.

This module caps it. Once an agent below quorum in an era has failed leases on
``FREE_RETRY_CAP_FAILED_LEASES`` distinct validators, the platform withdraws it
from that era with an ordinary audited :class:`ValidatorQueueWithdrawal`
(actor :data:`FREE_RETRY_CAP_ACTOR`). The miner's remedy is a paid re-run
(``ditto upload --allow-identical-rescore``) or a resubmission; an operator can
reinstate it, and an operator's reinstatement is never undone by this cap.

What counts comes only from recorded ticket outcomes, never from miner input:
expired scoring tickets whose retry budget is spent. Excluded, so they are
never capped or charged:

* ``provider_outage_parked`` leases (a fleet-wide provider outage);
* any lease on a validator that ran a ``dueling_season_rotation`` score
  re-test for the agent in this era (season rotation, catch-up and ATH-clear
  re-tests are platform-caused and always free);
* agents that already hold a quorum of scores in the era.

The cap is enforced only while the readmission fee is active
(:func:`~ditto.db.queries.readmission_gate.readmission_activation`): it exists
so free retries cannot be farmed once a re-run costs a fee.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Exists, Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    Score,
    ScoreAuditEntry,
    SubmissionRetirement,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.audit import SCORE_RETEST_EVENTS
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.queue_removal import removal_in_force
from ditto.db.queries.readmission_gate import current_readmission_activation
from ditto.db.queries.score_retests import DUELING_ROTATION_RETEST_BASIS
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.submission_settings import (
    latest_submission_settings,
    resolve_submission_settings,
)
from ditto.db.queries.terminal_failure import ticket_snapshot_wire
from ditto.db.queries.tickets import retry_budget_spent, scoring_retry_ticket

logger = logging.getLogger(__name__)

FREE_RETRY_CAP_FAILED_LEASES = 2 * SCORING_QUORUM
"""Failed leases on distinct validators an agent below quorum may use in one
era before it is withdrawn: twice the quorum, so a few honest infrastructure
failures never reach it."""
FREE_RETRY_CAP_ACTOR = "platform:free-retry-cap"
EXEMPT_FAILURE_DETAILS = frozenset({"provider_outage_parked"})


def _rotation_retest_on_lease() -> Exists:
    """A ``dueling_season_rotation`` re-test ran for this ticket's agent,
    validator and era (correlated to :class:`ValidatorTicket`)."""
    return (
        select(ScoreAuditEntry.seq)
        .where(
            ScoreAuditEntry.agent_id == ValidatorTicket.agent_id,
            ScoreAuditEntry.validator_hotkey == ValidatorTicket.validator_hotkey,
            ScoreAuditEntry.event.in_(sorted(SCORE_RETEST_EVENTS)),
            ScoreAuditEntry.payload["basis"].as_string()
            == DUELING_ROTATION_RETEST_BASIS,
            cast(ScoreAuditEntry.payload["bench_version"].as_string(), Integer)
            == ValidatorTicket.bench_version,
        )
        .correlate(ValidatorTicket)
        .exists()
    )


def _counted_failures() -> tuple[ColumnElement[bool], ...]:
    """Failed leases this cap counts, correlated to :class:`ValidatorTicket`."""
    return (
        ValidatorTicket.status == TicketStatus.EXPIRED,
        retry_budget_spent(),
        scoring_retry_ticket(),
        func.coalesce(ValidatorTicket.failure_detail, "").not_in(
            sorted(EXEMPT_FAILURE_DETAILS)
        ),
        ~_rotation_retest_on_lease(),
    )


async def free_retry_cap_candidates(
    session: AsyncSession, *, bench_version: int, limit: int = 200
) -> list[UUID]:
    """Evaluating agents below quorum in ``bench_version`` at or over the cap.

    Excludes agents with a live lease, an in-force withdrawal, or *any*
    earlier cap withdrawal for this era (an operator's reinstatement wins).
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
            ValidatorQueueWithdrawal.bench_version == bench_version,
            removal_in_force()
            | (ValidatorQueueWithdrawal.actor == FREE_RETRY_CAP_ACTOR),
        )
        .correlate(Agent)
        .exists()
    )
    scores = (
        select(func.count(Score.validator_hotkey))
        .where(Score.agent_id == Agent.agent_id, Score.bench_version == bench_version)
        .correlate(Agent)
        .scalar_subquery()
    )
    statement = (
        select(Agent.agent_id, Agent.created_at)
        .join(ValidatorTicket, ValidatorTicket.agent_id == Agent.agent_id)
        .where(
            Agent.status == AgentStatus.EVALUATING,
            ValidatorTicket.bench_version == bench_version,
            *_counted_failures(),
            ~live,
            ~withdrawn,
            scores < SCORING_QUORUM,
        )
        .group_by(Agent.agent_id, Agent.created_at)
        .having(
            func.count(func.distinct(ValidatorTicket.validator_hotkey))
            >= FREE_RETRY_CAP_FAILED_LEASES
        )
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(limit)
    )
    return [agent_id for agent_id, _ in await session.execute(statement)]


async def withdraw_at_free_retry_cap(
    session: AsyncSession, *, agent_id: UUID, bench_version: int, now: datetime
) -> ValidatorQueueWithdrawal | None:
    """Withdraw ``agent_id`` from ``bench_version`` if it is at the cap.

    Call inside a transaction. Locks the agent row then its tickets (the
    operator withdraw order) and re-decides under those locks.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None or agent.status != AgentStatus.EVALUATING:
        return None
    if agent_id not in await free_retry_cap_candidates(
        session, bench_version=bench_version, limit=10_000
    ):
        return None
    if await session.scalar(
        select(SubmissionRetirement.agent_id).where(
            SubmissionRetirement.agent_id == agent_id,
            SubmissionRetirement.bench_version == bench_version,
        )
    ):
        return None
    tickets = list(
        await session.scalars(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.bench_version == bench_version,
                scoring_retry_ticket(),
            )
            .order_by(ValidatorTicket.deadline, ValidatorTicket.validator_hotkey)
            .with_for_update()
        )
    )
    score_count = int(
        await session.scalar(
            select(func.count(Score.validator_hotkey)).where(
                Score.agent_id == agent_id, Score.bench_version == bench_version
            )
        )
        or 0
    )
    snapshot = [ticket_snapshot_wire(ticket) for ticket in tickets]
    digest = hashlib.sha256(
        json.dumps(
            {
                "actor": FREE_RETRY_CAP_ACTOR,
                "agent_id": str(agent_id),
                "artifact_sha256": agent.sha256,
                "bench_version": bench_version,
                "tickets": snapshot,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    withdrawal = ValidatorQueueWithdrawal(
        withdrawal_id=uuid.uuid4(),
        agent_id=agent_id,
        bench_version=bench_version,
        actor=FREE_RETRY_CAP_ACTOR,
        reason=(
            f"free re-leases used up: {FREE_RETRY_CAP_FAILED_LEASES} validators "
            "failed this agent without a scoring quorum; a paid re-run or a "
            "resubmission is the remedy (SN-485)"
        ),
        expected_snapshot=digest,
        score_count=score_count,
        ticket_snapshot=snapshot,
        created_at=now,
    )
    session.add(withdrawal)
    await session.flush()
    logger.warning(
        "free retry cap withdrew agent=%s bench_version=%s score_count=%s",
        agent_id,
        bench_version,
        score_count,
    )
    return withdrawal


async def sweep_free_retry_cap(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Apply the cap to every candidate in the active era, while it is active."""
    at = now or datetime.now(UTC)
    async with session_maker() as session:
        settings = resolve_submission_settings(
            await latest_submission_settings(session), payment_address=""
        )
        bench_version = await active_bench_version(session)
        activation = await current_readmission_activation(
            session, settings=settings, bench_version=bench_version, now=at
        )
        if not activation.active:
            return 0
        candidates = await free_retry_cap_candidates(
            session, bench_version=bench_version, limit=limit
        )
    written = 0
    for agent_id in candidates:
        try:
            async with session_maker() as session, session.begin():
                if await withdraw_at_free_retry_cap(
                    session, agent_id=agent_id, bench_version=bench_version, now=at
                ):
                    written += 1
        except Exception:
            logger.exception("free retry cap sweep failed agent=%s", agent_id)
    return written


__all__ = [
    "EXEMPT_FAILURE_DETAILS",
    "FREE_RETRY_CAP_ACTOR",
    "FREE_RETRY_CAP_FAILED_LEASES",
    "free_retry_cap_candidates",
    "sweep_free_retry_cap",
    "withdraw_at_free_retry_cap",
]

"""Audited recovery for submissions stranded by validator infrastructure.

The validator protocol currently reports lease progress but not a signed,
platform-verifiable terminal failure classification. Automatic infrastructure
retry would therefore guess from expiry and risk retrying deterministic miner
failures. This route requires an operator decision until that contract exists.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ditto.api_models.admin_validation_retry import (
    AdminBatchRetryRequest,
    AdminBatchRetryResponse,
    AdminBatchRetryResult,
    AdminEvictedLease,
    AdminReinstatementRetryBudget,
    AdminScoreOutlier,
    AdminScoreOutlierList,
    AdminScoreOutlierScore,
    AdminStuckSubmission,
    AdminStuckSubmissionsResponse,
    AdminV9ContractRetestItem,
    AdminV9ContractRetestList,
    AdminValidationQueueEviction,
    AdminValidationQueueEvictionRequest,
    AdminValidationQueueEvictionResponse,
    AdminValidationQueueReinstatement,
    AdminValidationQueueReinstatementRequest,
    AdminValidationQueueReinstatementResponse,
    AdminValidationQueueWithdrawal,
    AdminValidationQueueWithdrawalRequest,
    AdminValidationQueueWithdrawalResponse,
    AdminValidationRecovery,
    AdminValidationRetryDetail,
    AdminValidationRetryRequest,
    AdminValidationRetryResponse,
    AdminValidationTicket,
    AdminValidatorScoreReplacementDetail,
    AdminValidatorScoreReplacementRequest,
    AdminValidatorScoreReplacementResponse,
    AdminValidatorScoreRetestQueueRequest,
    AdminValidatorScoreRetestQueueResponse,
    AdminValidatorScoreRetestQueueResult,
    AdminValidatorScoreRetestReleaseRequest,
    AdminValidatorScoreRetestReleaseResponse,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.retry_state import RETRY_STATE_ORDER, RetryState
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.miner_logs import ticket_log_is_stale
from ditto.db.models import (
    Agent,
    BenchmarkRolloutMember,
    Score,
    ScoreAuditEntry,
    ValidatorHeartbeat,
    ValidatorQueueReinstatement,
    ValidatorQueueWithdrawal,
    ValidatorRetryRecovery,
    ValidatorTicket,
)
from ditto.db.queries.agents import list_agents_by_status
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_RELEASED,
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
    get_latest_score_retest_event,
)
from ditto.db.queries.benchmark_rollout import (
    MIN_SCOREABLE_BENCH_VERSION,
    active_bench_version,
    heartbeat_supports_version,
    open_rollout,
)
from ditto.db.queries.lease_liveness import (
    ACTION_OPERATOR_EVICTED,
    force_expire_lease,
    operator_eviction_liveness,
)
from ditto.db.queries.queue_removal import (
    is_in_force,
    load_queue_removal,
    load_reinstatement,
)
from ditto.db.queries.retry_budget import (
    MAX_AGENT_INFRA_RETRY_GRANTS,
    agent_infra_retry_grants,
)
from ditto.db.queries.retry_state import (
    classify_agent_retry_states,
    eviction_gate,
    is_exhausted,
    is_open_rollout_qualification,
    recovery_gate,
    reinstatement_gate,
    resolve_bench_version,
    withdrawal_gate,
    work_available_validator_hotkeys,
)
from ditto.db.queries.score_retests import (
    REPLACEMENT_TICKET_TTL,
    activate_next_score_retest,
    latest_retest_events_for_validator,
    retest_is_active,
    retest_is_open,
    retest_is_queued,
    score_retest_queue_positions,
)
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.tickets import ticket_attempt_cap
from ditto_screening_protocol.bench_v9 import (
    V9_SCORE_CONTRACT_MANIFEST_SHA256,
    V9_SCORE_CONTRACT_REVISION,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# How many EVALUATING agents to classify in one fleet sweep. The evaluating
# backlog is bounded (a few hundred at most); this caps a pathological scan.
_STUCK_SCAN_LIMIT = 2000

_REPLACEABLE_STATUSES = {
    AgentStatus.EVALUATING,
    AgentStatus.SCORED,
    AgentStatus.LIVE,
}
_CONTRACT_RETEST_STATUSES = _REPLACEABLE_STATUSES | {
    AgentStatus.ATH_PENDING_REVIEW,
}
_FINALIZED_STATUSES = {AgentStatus.SCORED, AgentStatus.LIVE}
OUTLIER_MIN_DEVIATION = 0.15
OUTLIER_GAP_RATIO = 2.0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ticket_wire(ticket: ValidatorTicket) -> dict[str, object]:
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


def _snapshot(
    *, agent: Agent, scores: list[Score], tickets: list[ValidatorTicket]
) -> str:
    payload = {
        "agent_id": str(agent.agent_id),
        "status": agent.status.value,
        "artifact_sha256": agent.sha256,
        "screening_policy_version": agent.screening_policy_version,
        "scores": [
            {
                "validator_hotkey": score.validator_hotkey,
                "run_id": score.run_id,
                "composite": score.composite,
                "signature": score.signature,
                "generated_at": _aware(score.generated_at).isoformat(
                    timespec="microseconds"
                ),
                "updated_at": _aware(score.updated_at).isoformat(
                    timespec="microseconds"
                ),
            }
            for score in sorted(
                scores, key=lambda item: (item.validator_hotkey, item.run_id)
            )
        ],
        "tickets": [_ticket_wire(ticket) for ticket in tickets],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _score_evidence(score: Score) -> dict[str, object]:
    return {
        "run_id": score.run_id,
        "seed": score.seed,
        "composite": score.composite,
        "tool_mean": score.tool_mean,
        "memory_mean": score.memory_mean,
        "median_ms": score.median_ms,
        "n": score.n,
        "bench_version": score.bench_version,
        "ticket_deadline": (
            score.details.get("ticket_deadline")
            if isinstance(score.details, dict)
            else None
        ),
        "signature": score.signature,
        "generated_at": _aware(score.generated_at).isoformat(),
    }


def _detect_outlier(scores: list[Score]) -> tuple[Score, str, float, float] | None:
    """Find one unambiguous extreme in a three-score median quorum.

    The extreme-to-median gap must be at least 0.15 and at least twice the
    other adjacent gap. This catches one validator far above or below a tight
    peer pair while declining to guess when all three scores are simply broad.
    """
    if len(scores) != SCORING_QUORUM:
        return None
    ordered = sorted(
        scores, key=lambda score: (score.composite, score.validator_hotkey)
    )
    low_gap = ordered[1].composite - ordered[0].composite
    high_gap = ordered[2].composite - ordered[1].composite
    if low_gap == high_gap:
        return None
    if low_gap > high_gap:
        deviation, peer_spread = low_gap, high_gap
        candidate, direction = ordered[0], "low"
    else:
        deviation, peer_spread = high_gap, low_gap
        candidate, direction = ordered[2], "high"
    if deviation < OUTLIER_MIN_DEVIATION:
        return None
    if deviation < OUTLIER_GAP_RATIO * peer_spread:
        return None
    return candidate, direction, deviation, peer_spread


def _outlier_score(score: Score) -> AdminScoreOutlierScore:
    return AdminScoreOutlierScore(
        validator_hotkey=score.validator_hotkey,
        run_id=score.run_id,
        composite=score.composite,
    )


def _v9_contract_observation(
    score: Score,
) -> tuple[str | None, str | None, str | None, int | None]:
    details = score.details if isinstance(score.details, dict) else {}
    base = details.get("v9_base")
    if not isinstance(base, dict):
        return None, None, None, None
    contract = base.get("score_contract")
    revision = contract.get("revision") if isinstance(contract, dict) else None
    manifest = contract.get("manifest_sha256") if isinstance(contract, dict) else None
    gates = base.get("score_gates")
    rollout_mode = gates.get("rollout_mode") if isinstance(gates, dict) else None
    factor = base.get("semantic_gate_factor_bps")
    return (
        revision if isinstance(revision, str) else None,
        manifest if isinstance(manifest, str) else None,
        rollout_mode if isinstance(rollout_mode, str) else None,
        factor if type(factor) is int else None,
    )


def _is_v9_contract_mismatch(score: Score) -> bool:
    if score.bench_version != 9:
        return False
    revision, manifest, rollout_mode, factor = _v9_contract_observation(score)
    if (
        revision != V9_SCORE_CONTRACT_REVISION
        or manifest != V9_SCORE_CONTRACT_MANIFEST_SHA256
        or rollout_mode != "enforce"
    ):
        return True

    # v0.53.1 could publish the authoritative enforce contract while still
    # losing every per-case inference attribution: a harness /run response may
    # finish just before the source-bound broker copies its final upstream
    # bytes and decrements inFlight. Aggregate trusted accounting then proves
    # successful model use, but the all-or-nothing case window reports
    # insufficient evidence and zeros the score. This is deterministic repair,
    # not outlier selection. Never include a genuine zero-inference result.
    details = score.details if isinstance(score.details, dict) else {}
    base = details.get("v9_base")
    gates = base.get("score_gates") if isinstance(base, dict) else None
    model_use = gates.get("model_use") if isinstance(gates, dict) else None
    return bool(
        factor == 0
        and isinstance(model_use, dict)
        and model_use.get("result") == "insufficient_evidence"
        and model_use.get("case_attribution_complete") is False
        and type(model_use.get("successful_requests")) is int
        and model_use["successful_requests"] > 0
    )


def _recovery_item(row: ValidatorRetryRecovery) -> AdminValidationRecovery:
    return AdminValidationRecovery(
        recovery_id=row.recovery_id,
        agent_id=row.agent_id,
        actor=row.actor,
        reason=row.reason,
        score_count=row.score_count,
        bench_version=row.bench_version,
        expected_snapshot=row.expected_snapshot,
        granted_validator_hotkeys=list(row.granted_validator_hotkeys),
        created_at=row.created_at,
    )


def _withdrawal_item(
    row: ValidatorQueueWithdrawal,
) -> AdminValidationQueueWithdrawal:
    return AdminValidationQueueWithdrawal(
        withdrawal_id=row.withdrawal_id,
        agent_id=row.agent_id,
        bench_version=row.bench_version,
        actor=row.actor,
        reason=row.reason,
        expected_snapshot=row.expected_snapshot,
        score_count=row.score_count,
        evicted_validator_hotkeys=row.evicted_validator_hotkeys,
        created_at=row.created_at,
        reinstated_at=row.reinstated_at,
    )


def _reinstatement_item(
    row: ValidatorQueueReinstatement,
) -> AdminValidationQueueReinstatement:
    return AdminValidationQueueReinstatement(
        reinstatement_id=row.reinstatement_id,
        withdrawal_id=row.withdrawal_id,
        agent_id=row.agent_id,
        bench_version=row.bench_version,
        actor=row.actor,
        reason=row.reason,
        expected_snapshot=row.expected_snapshot,
        score_count=row.score_count,
        retry_budget_snapshot=AdminReinstatementRetryBudget.model_validate(
            row.retry_budget_snapshot
        ),
        created_at=row.created_at,
    )


def _eviction_item(row: ValidatorQueueWithdrawal) -> AdminValidationQueueEviction:
    return AdminValidationQueueEviction(
        eviction_id=row.withdrawal_id,
        agent_id=row.agent_id,
        bench_version=row.bench_version,
        actor=row.actor,
        reason=row.reason,
        expected_snapshot=row.expected_snapshot,
        score_count=row.score_count,
        evicted_validator_hotkeys=list(row.evicted_validator_hotkeys or []),
        created_at=row.created_at,
        reinstated_at=row.reinstated_at,
    )


def _silently_expired(ticket: ValidatorTicket) -> bool:
    """The lease ran out with nothing reported about *this* attempt.

    ``failure_reason`` survives a reissue on purpose (see
    ``docs/validator-retry-model.md``), so a stale reason attached to a
    superseded lease must not read as "this attempt reported a failure" — hence
    the ``failed_at < issued_at`` arm rather than a bare null check.
    """
    if ticket.status != TicketStatus.EXPIRED:
        return False
    if ticket.failure_reason is None or ticket.failed_at is None:
        return True
    return _aware(ticket.failed_at) < _aware(ticket.issued_at)


def _ticket_item(ticket: ValidatorTicket) -> AdminValidationTicket:
    return AdminValidationTicket(
        validator_hotkey=ticket.validator_hotkey,
        slot_id=ticket.slot_id,
        status=ticket.status.value,  # type: ignore[arg-type]
        issued_at=ticket.issued_at,
        deadline=ticket.deadline,
        bench_version=ticket.bench_version,
        attempt_count=ticket.attempt_count,
        manual_retry_grants=ticket.manual_retry_grants,
        infra_retry_grants=ticket.infra_retry_grants,
        retry_after=ticket.retry_after,
        retry_budget_exhausted=(
            ticket.status == TicketStatus.EXPIRED
            and ticket.attempt_count >= ticket_attempt_cap(ticket)
        ),
        failure_reason=ticket.failure_reason,
        failed_at=ticket.failed_at,
        failure_detail=ticket.failure_detail,
        container_log_tail=ticket.container_log_tail,
        container_log_tail_attempt=ticket.container_log_tail_attempt,
        container_log_tail_stale=ticket_log_is_stale(ticket),
        silently_expired=_silently_expired(ticket),
    )


async def _load(
    session: AsyncSession,
    *,
    agent_id: UUID,
    for_update: bool,
    required_bench_version: int | None = None,
) -> tuple[
    Agent | None,
    int,
    list[Score],
    list[ValidatorTicket],
    list[ValidatorRetryRecovery],
]:
    agent_query = select(Agent).where(Agent.agent_id == agent_id)
    if for_update:
        agent_query = agent_query.with_for_update()
    agent = await session.scalar(agent_query)
    if agent is None:
        return None, 2, [], [], []
    all_scores = list(
        (
            await session.scalars(
                select(Score)
                .where(Score.agent_id == agent_id)
                .order_by(Score.validator_hotkey.asc(), Score.run_id.asc())
            )
        ).all()
    )
    ticket_query = (
        select(ValidatorTicket)
        .where(ValidatorTicket.agent_id == agent_id)
        .order_by(
            ValidatorTicket.deadline.asc(), ValidatorTicket.validator_hotkey.asc()
        )
    )
    if for_update:
        ticket_query = ticket_query.with_for_update()
    all_tickets = list((await session.scalars(ticket_query)).all())
    canonical_version = await active_bench_version(session)
    bench_version = (
        required_bench_version
        if required_bench_version is not None
        else resolve_bench_version(
            all_tickets=all_tickets,
            all_scores=all_scores,
            canonical_version=canonical_version,
        )
    )
    scores = [score for score in all_scores if score.bench_version == bench_version]
    tickets = [
        ticket for ticket in all_tickets if ticket.bench_version == bench_version
    ]
    recoveries = list(
        (
            await session.scalars(
                select(ValidatorRetryRecovery)
                .where(
                    ValidatorRetryRecovery.agent_id == agent_id,
                    ValidatorRetryRecovery.bench_version == bench_version,
                )
                .order_by(
                    ValidatorRetryRecovery.created_at.asc(),
                    ValidatorRetryRecovery.recovery_id.asc(),
                )
            )
        ).all()
    )
    return agent, bench_version, scores, tickets, recoveries


async def _load_withdrawal(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    for_update: bool,
) -> ValidatorQueueWithdrawal | None:
    """The removal currently keeping this submission out of the era's queue.

    In-force only: a reinstated row is not a removal any more, so every gate
    that reads this treats a reinstated submission exactly like one that was
    never evicted.
    """
    return await load_queue_removal(
        session,
        agent_id=agent_id,
        bench_version=bench_version,
        for_update=for_update,
        in_force_only=True,
    )


@router.get("/validation-retries", response_model=AdminStuckSubmissionsResponse)
async def list_validation_retries(
    _admin: AdminDep,
    session: SessionDep,
    state: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminStuckSubmissionsResponse:
    """Fleet-wide triage of every below-quorum submission and why it is stuck.

    The single-agent detail route answers "why is *this* one stuck?"; this
    answers "which ones need me?" without a per-agent sweep. Filter with one or
    more ``state`` query params (e.g. ``?state=exhausted``); ``counts`` always
    reflects the whole fleet so a filtered view still shows the totals. The
    list is paginated after the stable triage sort, and complete ticket history
    stays on the single-agent detail route.
    """
    now = datetime.now(UTC)
    requested_states = set(state or [])
    unknown = requested_states - set(RETRY_STATE_ORDER)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="unknown retry state: " + ", ".join(sorted(unknown)),
        )

    agents = await list_agents_by_status(
        session, statuses=[AgentStatus.EVALUATING], limit=_STUCK_SCAN_LIMIT
    )
    rollout = await open_rollout(session)
    if rollout is not None:
        rollout_agents = list(
            await session.scalars(
                select(Agent)
                .join(
                    BenchmarkRolloutMember,
                    BenchmarkRolloutMember.agent_id == Agent.agent_id,
                )
                .where(
                    BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                    Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
                )
                .order_by(BenchmarkRolloutMember.position)
            )
        )
        known = {agent.agent_id for agent in agents}
        agents.extend(agent for agent in rollout_agents if agent.agent_id not in known)
    classified = await classify_agent_retry_states(
        session,
        agents=agents,
        now=now,
        require_work_available_validator=True,
    )
    agents_by_id = {agent.agent_id: agent for agent in agents}

    submissions: list[AdminStuckSubmission] = []
    counts: dict[RetryState, int] = {}
    for agent_id, retry in classified.items():
        counts[retry.state] = counts.get(retry.state, 0) + 1
        if requested_states and retry.state not in requested_states:
            continue
        agent = agents_by_id[agent_id]
        scored_hotkeys = {s.validator_hotkey for s in retry.scores}
        submissions.append(
            AdminStuckSubmission(
                agent_id=agent.agent_id,
                miner_hotkey=agent.miner_hotkey,
                agent_name=agent.name,
                agent_version=agent.version,
                bench_version=retry.bench_version,
                score_count=retry.score_count,
                quorum=SCORING_QUORUM,
                retry_state=retry.state,
                automatic_retry_available=retry.automatic_retry_available,
                recovery_allowed=retry.recovery_allowed,
                blocking_reason=retry.blocking_reason,
                earliest_retry_after=retry.earliest_retry_after,
                attempts_used=max((t.attempt_count for t in retry.tickets), default=0),
                exhausted_validator_count=sum(
                    1
                    for t in retry.tickets
                    if is_exhausted(t) and t.validator_hotkey not in scored_hotkeys
                ),
                silent_expiry_count=sum(
                    1 for t in retry.tickets if _silently_expired(t)
                ),
                snapshot=_snapshot(
                    agent=agent, scores=retry.scores, tickets=retry.tickets
                ),
                ticket_states=dict(Counter(t.status.value for t in retry.tickets)),
            )
        )

    submissions.sort(
        key=lambda item: (
            RETRY_STATE_ORDER[item.retry_state],
            item.earliest_retry_after or datetime.max.replace(tzinfo=UTC),
            item.agent_id,
        )
    )
    count = len(submissions)
    page = submissions[offset : offset + limit]
    return AdminStuckSubmissionsResponse(
        generated_at=now,
        quorum=SCORING_QUORUM,
        counts=counts,
        count=count,
        returned=len(page),
        limit=limit,
        offset=offset,
        has_more=offset + len(page) < count,
        submissions=page,
    )


@router.get("/validation-retries/{agent_id}", response_model=AdminValidationRetryDetail)
async def get_validation_retry(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminValidationRetryDetail:
    agent, bench_version, scores, tickets, recoveries = await _load(
        session, agent_id=agent_id, for_update=False
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    score_count = len(scores)
    now = datetime.now(UTC)
    automatic, allowed, reason, _ = recovery_gate(
        agent=agent,
        scores=scores,
        tickets=tickets,
        now=now,
        bench_version=bench_version,
        rollout_qualification=await is_open_rollout_qualification(
            session, agent_id=agent.agent_id, bench_version=bench_version
        ),
        work_available_hotkeys=await work_available_validator_hotkeys(session, now=now),
    )
    # The most recent removal, reversed or not: the operator surface has to be
    # able to show an eviction that was undone, while every gate below asks the
    # narrower question of whether a removal is still in force.
    latest_removal = await load_queue_removal(
        session,
        agent_id=agent.agent_id,
        bench_version=bench_version,
        for_update=False,
        in_force_only=False,
    )
    withdrawal = latest_removal if is_in_force(latest_removal) else None
    reinstatement = (
        await load_reinstatement(session, withdrawal_id=latest_removal.withdrawal_id)
        if latest_removal is not None
        else None
    )
    reinstatement_allowed, reinstatement_reason = reinstatement_gate(
        agent=agent,
        scores=scores,
        removal=latest_removal,
        bench_version=bench_version,
        active_version=await active_bench_version(session),
    )
    withdrawal_allowed, withdrawal_reason = withdrawal_gate(
        agent=agent,
        scores=scores,
        tickets=tickets,
        bench_version=bench_version,
    )
    withdrawal_allowed = withdrawal_allowed and withdrawal is None
    withdrawal_blocking_reason = (
        "submission is already removed from this benchmark queue"
        if withdrawal is not None
        else withdrawal_reason
    )
    eviction_allowed, eviction_reason = eviction_gate(agent=agent, scores=scores)
    eviction_allowed = eviction_allowed and withdrawal is None
    eviction_blocking_reason = (
        "submission is already removed from this benchmark queue"
        if withdrawal is not None
        else eviction_reason
    )
    return AdminValidationRetryDetail(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        agent_name=agent.name,
        agent_version=agent.version,
        agent_status=agent.status.value,
        score_count=score_count,
        quorum=SCORING_QUORUM,
        snapshot=_snapshot(agent=agent, scores=scores, tickets=tickets),
        automatic_retry_available=automatic,
        recovery_allowed=allowed and withdrawal is None,
        blocking_reason=(
            "submission is removed from this benchmark queue"
            if withdrawal is not None
            else reason
        ),
        withdrawal_allowed=withdrawal_allowed,
        withdrawal_blocking_reason=withdrawal_blocking_reason,
        eviction_allowed=eviction_allowed,
        eviction_blocking_reason=eviction_blocking_reason,
        reinstatement_allowed=reinstatement_allowed,
        reinstatement_blocking_reason=reinstatement_reason,
        live_ticket_count=sum(
            1 for ticket in tickets if ticket.status == TicketStatus.ISSUED
        ),
        withdrawal=(
            _withdrawal_item(latest_removal) if latest_removal is not None else None
        ),
        reinstatement=(
            _reinstatement_item(reinstatement) if reinstatement is not None else None
        ),
        tickets=[_ticket_item(ticket) for ticket in tickets],
        recoveries=[_recovery_item(row) for row in recoveries],
    )


async def _apply_recovery(
    session: AsyncSession,
    *,
    agent_id: UUID,
    actor: str,
    reason: str,
    request_id: UUID,
    expected_snapshot: str,
    now: datetime,
) -> tuple[str, str | None, ValidatorRetryRecovery | None]:
    """Grant one audited recovery inside the caller's transaction.

    Returns ``(status, detail, recovery)`` and never raises for an expected
    condition, so the batch route can collect a per-agent verdict instead of
    aborting the whole set. ``status`` is ``granted`` | ``idempotent`` |
    ``skipped``; ``detail`` explains a skip.
    """
    # Ticket issuance locks the rollout before a ticket row. Preserve that lock
    # order here so an operator grant cannot deadlock a validator claim while
    # also preventing the rollout from activating underneath the grant.
    rollout = await open_rollout(session, for_update=True)
    agent, bench_version, scores, tickets, recoveries = await _load(
        session, agent_id=agent_id, for_update=True
    )
    if agent is None:
        return "skipped", "agent not found", None
    existing = await session.get(ValidatorRetryRecovery, request_id)
    if existing is not None:
        if (
            existing.agent_id != agent_id
            or existing.actor != actor
            or existing.reason != reason
            or existing.expected_snapshot != expected_snapshot
        ):
            return "skipped", "request id already used", None
        return "idempotent", None, existing

    if (
        await _load_withdrawal(
            session,
            agent_id=agent_id,
            bench_version=bench_version,
            for_update=True,
        )
        is not None
    ):
        return "skipped", "submission was removed from this benchmark queue", None

    current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
    if current_snapshot != expected_snapshot:
        return "skipped", "validation state changed", None
    rollout_qualification = bool(
        rollout is not None
        and rollout.desired_version == bench_version
        and await session.scalar(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.agent_id == agent.agent_id,
            )
        )
    )
    _, allowed, gate_reason, selected = recovery_gate(
        agent=agent,
        scores=scores,
        tickets=tickets,
        now=now,
        bench_version=bench_version,
        rollout_qualification=rollout_qualification,
        work_available_hotkeys=await work_available_validator_hotkeys(session, now=now),
    )
    if not allowed:
        return "skipped", gate_reason or "retry unavailable", None

    ticket_snapshot = [_ticket_wire(ticket) for ticket in tickets]
    for ticket in selected:
        # Old retry loops could advance attempt_count far beyond the cap before
        # issuance started enforcing it. Preserve that history, but raise the
        # cap by the minimum amount that restores exactly one future lease.
        ticket.manual_retry_grants += (
            ticket.attempt_count - ticket_attempt_cap(ticket) + 1
        )
        ticket.retry_after = now
    recovery = ValidatorRetryRecovery(
        recovery_id=request_id,
        agent_id=agent_id,
        actor=actor,
        reason=reason,
        expected_snapshot=current_snapshot,
        score_count=len(scores),
        bench_version=bench_version,
        ticket_snapshot=ticket_snapshot,
        granted_validator_hotkeys=[ticket.validator_hotkey for ticket in selected],
        created_at=now,
    )
    session.add(recovery)
    await session.flush()
    return "granted", None, recovery


def _require_actor(x_admin_actor: str | None) -> str:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


@router.post(
    "/validation-retries/{agent_id}/withdraw",
    response_model=AdminValidationQueueWithdrawalResponse,
)
async def withdraw_failed_validation_from_queue(
    agent_id: UUID,
    payload: AdminValidationQueueWithdrawalRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidationQueueWithdrawalResponse:
    """Stop benchmark-scoped assignment while preserving every source record."""

    actor = _require_actor(x_admin_actor)
    async with session.begin():
        agent, bench_version, scores, tickets, _recoveries = await _load(
            session, agent_id=agent_id, for_update=True
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

        existing_request = await session.get(
            ValidatorQueueWithdrawal, payload.request_id
        )
        if existing_request is not None:
            if (
                existing_request.agent_id != agent_id
                or existing_request.actor != actor
                or existing_request.reason != payload.reason
                or existing_request.expected_snapshot != payload.expected_snapshot
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            return AdminValidationQueueWithdrawalResponse(
                withdrawal=_withdrawal_item(existing_request), idempotent=True
            )

        existing = await _load_withdrawal(
            session,
            agent_id=agent_id,
            bench_version=bench_version,
            for_update=True,
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="submission is already removed from this benchmark queue",
            )

        current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(status_code=409, detail="validation state changed")

        allowed, reason = withdrawal_gate(
            agent=agent,
            scores=scores,
            tickets=tickets,
            bench_version=bench_version,
        )
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail=reason or "queue withdrawal unavailable",
            )

        withdrawal = ValidatorQueueWithdrawal(
            withdrawal_id=payload.request_id,
            agent_id=agent_id,
            bench_version=bench_version,
            actor=actor,
            reason=payload.reason,
            expected_snapshot=current_snapshot,
            score_count=len(scores),
            ticket_snapshot=[_ticket_wire(ticket) for ticket in tickets],
            created_at=datetime.now(UTC),
        )
        session.add(withdrawal)
        await session.flush()
        return AdminValidationQueueWithdrawalResponse(
            withdrawal=_withdrawal_item(withdrawal), idempotent=False
        )


@router.post(
    "/validation-retries/{agent_id}/evict",
    response_model=AdminValidationQueueEvictionResponse,
)
async def evict_submission_from_validator_queue(
    agent_id: UUID,
    payload: AdminValidationQueueEvictionRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidationQueueEvictionResponse:
    """Revoke a submission's live leases now, and close its benchmark era.

    The escape hatch for a submission that is actively starving the fleet.
    ``/withdraw`` refuses anything that ``can still reach quorum automatically``
    or that ``a validator ticket is still active``, which means it can only ever
    clean up a submission that had *already* stopped consuming capacity. On
    2026-07-27 that left an operator watching three hanging agents hold half the
    fleet's validator slots for 90 minutes at a time with no way to intervene.

    Eviction reaches the same terminal state — the same
    ``validator_queue_withdrawals`` row every queue predicate already reads — but
    first force-expires each live lease through the shared
    :func:`~ditto.db.queries.lease_liveness.force_expire_lease` chokepoint, so
    the slots return to the pool on the validators' next poll instead of at the
    90-minute deadline. Each revoked lease gets its own ``validator_lease_audit``
    row with the operator's name and reason, recorded under the distinct action
    ``operator_evicted``.

    Nothing is deleted: the submission, the miner's payment, the artifact, the
    screening verdict, every accepted score and the whole ticket history all
    survive, exactly as with a withdrawal. This is not a rejection and not a
    rescreen. A later benchmark era is a fresh eligibility decision.

    A validator still mid-run on an evicted lease is not broken by this: its
    ticket is no longer ``issued`` and its deadline has moved, so a late score
    fails the k=3 gate in ``submit_score`` and is refused with a 409 — the same
    clean refusal any superseded lease already gets. No score reaches the ledger.
    """

    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    async with session.begin():
        agent, bench_version, scores, tickets, _recoveries = await _load(
            session, agent_id=agent_id, for_update=True
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

        # Idempotency is the withdrawal route's, deliberately: the two write the
        # same table, so replaying either request id has to behave identically.
        existing_request = await session.get(
            ValidatorQueueWithdrawal, payload.request_id
        )
        if existing_request is not None:
            if (
                existing_request.agent_id != agent_id
                or existing_request.actor != actor
                or existing_request.reason != payload.reason
                or existing_request.expected_snapshot != payload.expected_snapshot
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            hotkeys = list(existing_request.evicted_validator_hotkeys or [])
            return AdminValidationQueueEvictionResponse(
                eviction=_eviction_item(existing_request),
                # The leases are already gone; their audit rows are the record.
                evicted_leases=[],
                freed_slots=len(hotkeys),
                idempotent=True,
            )

        existing = await _load_withdrawal(
            session,
            agent_id=agent_id,
            bench_version=bench_version,
            for_update=True,
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="submission is already removed from this benchmark queue",
            )

        current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(status_code=409, detail="validation state changed")

        allowed, reason = eviction_gate(agent=agent, scores=scores)
        if not allowed:
            raise HTTPException(
                status_code=409, detail=reason or "queue eviction unavailable"
            )

        # Snapshot the ticket set BEFORE the revocation rewrites deadlines, so
        # the audit record preserves what the operator actually acted on.
        ticket_snapshot = [_ticket_wire(ticket) for ticket in tickets]
        live = [ticket for ticket in tickets if ticket.status == TicketStatus.ISSUED]
        evicted: list[AdminEvictedLease] = []
        for ticket in sorted(live, key=lambda item: item.validator_hotkey):
            original_deadline = _aware(ticket.deadline)
            audit = await force_expire_lease(
                session,
                ticket=ticket,
                now=now,
                # Not an inferred idleness verdict. See
                # operator_eviction_liveness: it reads protocol-16's positive
                # "occupied, not progressing" report rather than inferring from
                # a slot's absence, and it relaxes nothing in the automatic gate.
                liveness=await operator_eviction_liveness(
                    session,
                    ticket=ticket,
                    now=now,
                    actor=actor,
                    reason=payload.reason,
                    request_id=payload.request_id,
                ),
                context="admin_queue_eviction",
                action=ACTION_OPERATOR_EVICTED,
                # THE correctness property of this feature. The no-fault grant
                # offsets the attempt a coming reissue would charge; an eviction
                # is the decision that there is no reissue. Granting anyway would
                # raise the cap on the artifact just evicted and rebuild the
                # amplifier that produced mnemox-v55's 9 attempts against a base
                # budget of 2 (ditto-subnet#279).
                compensate=False,
            )
            evicted.append(
                AdminEvictedLease(
                    validator_hotkey=ticket.validator_hotkey,
                    slot_id=ticket.slot_id,
                    bench_version=ticket.bench_version,
                    issued_at=_aware(ticket.issued_at),
                    original_deadline=original_deadline,
                    attempt_count=ticket.attempt_count,
                    audit_id=audit.audit_id,
                )
            )

        eviction = ValidatorQueueWithdrawal(
            withdrawal_id=payload.request_id,
            agent_id=agent_id,
            bench_version=bench_version,
            actor=actor,
            reason=payload.reason,
            expected_snapshot=current_snapshot,
            score_count=len(scores),
            ticket_snapshot=ticket_snapshot,
            evicted_validator_hotkeys=[item.validator_hotkey for item in evicted],
            created_at=now,
        )
        session.add(eviction)
        await session.flush()
        return AdminValidationQueueEvictionResponse(
            eviction=_eviction_item(eviction),
            evicted_leases=evicted,
            freed_slots=len(evicted),
            idempotent=False,
        )


async def _retry_budget_snapshot(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    tickets: list[ValidatorTicket],
    recovery_count: int,
) -> AdminReinstatementRetryBudget:
    """Read the attempt budget a reinstatement is about to leave untouched.

    Recorded on the reinstatement row so "nothing was granted" is checkable after
    the fact rather than merely asserted in a docstring.
    """
    return AdminReinstatementRetryBudget(
        attempts_used=max((ticket.attempt_count for ticket in tickets), default=0),
        agent_infra_retry_grants=await agent_infra_retry_grants(
            session, agent_id=agent_id, bench_version=bench_version
        ),
        max_agent_infra_retry_grants=MAX_AGENT_INFRA_RETRY_GRANTS,
        manual_retry_grants=sum(ticket.manual_retry_grants for ticket in tickets),
        operator_recoveries=recovery_count,
        max_operator_recoveries=None,
    )


@router.post(
    "/validation-retries/{agent_id}/reinstate",
    response_model=AdminValidationQueueReinstatementResponse,
)
async def reinstate_removed_submission_to_validator_queue(
    agent_id: UUID,
    payload: AdminValidationQueueReinstatementRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidationQueueReinstatementResponse:
    """Undo an operator queue removal, with no retry budget added.

    Eviction (#515) shipped one-way, and that is the only reason it went unused
    against the submissions it was built for. An evicted miner has paid an
    evaluation fee for a benchmark era that an operator ended, and without this
    route their only way back is a fresh submission and a second fee — so a
    capacity decision doubles as a fine, and taking it requires being sure the
    miner deserved one. The ``mnemo*`` family that prompted #515 was expensive,
    not shown to be malicious: a source review found self-imposed per-case
    deadlines, no hang primitives, and a version history of pure latency work.

    Ordinary exhausted-submission withdrawals use the same reversal. Restoring
    one does not make an exhausted ticket leaseable by itself; it only removes
    the queue block so a separately bounded, audited operator retry can proceed.

    What this restores is exactly the queue effect and nothing else. The removal
    row is resolved (``reinstated_at``), so
    :func:`~ditto.db.queries.benchmark_admission.validator_queue_admission_predicate`
    admits the agent again and its expired tickets are re-leasable on their
    ordinary terms. What it deliberately does **not** do:

    * resurrect the revoked leases — those slots were handed to other
      submissions on the validators' next poll and are not the platform's to
      take back;
    * touch ``attempt_count``, ``manual_retry_grants`` or ``infra_retry_grants``;
    * delete or rewrite the operator-recovery audit history for the era.

    That inertness is the security property, not an omission. Eviction refuses to
    compensate the miner (``compensate=False``) precisely so it cannot raise the
    attempt cap on the artifact it just evicted; if reinstatement handed the cap
    back, the pair would become an attempt printer and a miner could be evicted
    and reinstated in a loop to farm free leases past
    :data:`~ditto.db.queries.retry_budget.MAX_AGENT_INFRA_RETRY_GRANTS` (12 per
    agent per era, #522) — rebuilding the exact amplifier that took
    ``mnemox-v55`` to nine attempts against a base budget of two. Both bounds are
    keyed on (agent, benchmark version) and this route writes neither, so the
    cycle is budget-neutral however many times it runs. An operator who *does*
    want to hand back attempts still has one route for it, ``/retry``, which is
    snapshot-guarded, grants only the minimum budget for one future lease per
    selected validator ticket, and is audited per action. The counts as they
    stood are recorded on the reinstatement row.

    The eviction record is preserved, never deleted: the leases it revoked each
    carry a ``validator_lease_audit`` row that
    ``GET /admin/lease-revocations?action=operator_evicted`` still reads, and an
    eviction that was later reversed is a fact worth keeping. This writes its own
    append-only row with its own actor, reason and idempotency key instead.
    """

    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    async with session.begin():
        agent, bench_version, scores, tickets, recoveries = await _load(
            session, agent_id=agent_id, for_update=True
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

        existing_request = await session.get(
            ValidatorQueueReinstatement, payload.request_id
        )
        if existing_request is not None:
            if (
                existing_request.agent_id != agent_id
                or existing_request.actor != actor
                or existing_request.reason != payload.reason
                or existing_request.expected_snapshot != payload.expected_snapshot
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            reversed_row = await session.get(
                ValidatorQueueWithdrawal, existing_request.withdrawal_id
            )
            assert reversed_row is not None
            return AdminValidationQueueReinstatementResponse(
                reinstatement=_reinstatement_item(existing_request),
                eviction=_eviction_item(reversed_row),
                restored_bench_version=existing_request.bench_version,
                idempotent=True,
            )

        removal = await load_queue_removal(
            session,
            agent_id=agent_id,
            bench_version=bench_version,
            for_update=True,
            in_force_only=False,
        )
        current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(status_code=409, detail="validation state changed")

        allowed, reason = reinstatement_gate(
            agent=agent,
            scores=scores,
            removal=removal,
            bench_version=bench_version,
            active_version=await active_bench_version(session),
        )
        if not allowed:
            raise HTTPException(
                status_code=409, detail=reason or "reinstatement unavailable"
            )
        assert removal is not None

        removal.reinstated_at = now
        reinstatement = ValidatorQueueReinstatement(
            reinstatement_id=payload.request_id,
            withdrawal_id=removal.withdrawal_id,
            agent_id=agent_id,
            bench_version=bench_version,
            actor=actor,
            reason=payload.reason,
            expected_snapshot=current_snapshot,
            score_count=len(scores),
            ticket_snapshot=[_ticket_wire(ticket) for ticket in tickets],
            retry_budget_snapshot=(
                await _retry_budget_snapshot(
                    session,
                    agent_id=agent_id,
                    bench_version=bench_version,
                    tickets=tickets,
                    recovery_count=len(recoveries),
                )
            ).model_dump(mode="json"),
            created_at=now,
        )
        session.add(reinstatement)
        await session.flush()
        return AdminValidationQueueReinstatementResponse(
            reinstatement=_reinstatement_item(reinstatement),
            eviction=_eviction_item(removal),
            restored_bench_version=bench_version,
            idempotent=False,
        )


@router.post(
    "/validation-retries/{agent_id}/retry",
    response_model=AdminValidationRetryResponse,
)
async def retry_validation_after_infrastructure_failure(
    agent_id: UUID,
    payload: AdminValidationRetryRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidationRetryResponse:
    actor = _require_actor(x_admin_actor)
    async with session.begin():
        status, detail, recovery = await _apply_recovery(
            session,
            agent_id=agent_id,
            actor=actor,
            reason=payload.reason,
            request_id=payload.request_id,
            expected_snapshot=payload.expected_snapshot,
            now=datetime.now(UTC),
        )
        if status == "skipped":
            code = 404 if detail == "agent not found" else 409
            raise HTTPException(status_code=code, detail=detail or "retry unavailable")
        assert recovery is not None
        return AdminValidationRetryResponse(
            recovery=_recovery_item(recovery), idempotent=status == "idempotent"
        )


@router.post(
    "/validation-retries/batch-retry",
    response_model=AdminBatchRetryResponse,
)
async def batch_retry_validation(
    payload: AdminBatchRetryRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminBatchRetryResponse:
    """Grant recoveries to several stranded submissions in one operator action.

    Each item is gated and snapshot-checked exactly like the single-agent route;
    an item whose state moved (or that no longer qualifies) is skipped with a
    reason rather than force-granted. All grants commit together.
    """
    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    results: list[AdminBatchRetryResult] = []
    async with session.begin():
        for item in payload.items:
            status, detail, recovery = await _apply_recovery(
                session,
                agent_id=item.agent_id,
                actor=actor,
                reason=payload.reason,
                request_id=item.request_id,
                expected_snapshot=item.expected_snapshot,
                now=now,
            )
            results.append(
                AdminBatchRetryResult(
                    agent_id=item.agent_id,
                    status=status,  # type: ignore[arg-type]
                    detail=detail,
                    recovery=_recovery_item(recovery) if recovery is not None else None,
                )
            )
    granted = sum(1 for result in results if result.status == "granted")
    return AdminBatchRetryResponse(granted=granted, results=results)


async def _replacement_state(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    for_update: bool,
    required_bench_version: int | None = None,
) -> tuple[Agent | None, int, list[Score], list[ValidatorTicket], Score | None]:
    agent, bench_version, scores, tickets, _ = await _load(
        session,
        agent_id=agent_id,
        for_update=for_update,
        required_bench_version=required_bench_version,
    )
    target = next(
        (score for score in scores if score.validator_hotkey == validator_hotkey),
        None,
    )
    return agent, bench_version, scores, tickets, target


async def _validator_busy_elsewhere(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
) -> bool:
    return (
        await session.scalar(
            select(ValidatorTicket.agent_id).where(
                ValidatorTicket.validator_hotkey == validator_hotkey,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.agent_id != agent_id,
            )
        )
        is not None
    )


def _replacement_gate(
    *,
    agent: Agent,
    target: Score | None,
    ticket: ValidatorTicket | None,
    validator_busy: bool,
    replacement_open: bool = False,
) -> str | None:
    if agent.status not in _REPLACEABLE_STATUSES:
        return "submission is not in a scoreable state"
    if target is None:
        return "validator has no accepted score to replace"
    if replacement_open:
        return "replacement score is already queued or pending"
    if ticket is None or ticket.status != TicketStatus.SCORED:
        return "accepted score is not backed by a consumed validator ticket"
    if validator_busy:
        return "validator is currently assigned to another submission"
    return None


def _queue_gate(
    *,
    agent: Agent,
    target: Score | None,
    ticket: ValidatorTicket | None,
    replacement_open: bool,
) -> str | None:
    if agent.status not in _FINALIZED_STATUSES:
        return "submission is no longer finalized"
    if target is None:
        return "validator has no accepted score to replace"
    if replacement_open:
        return "replacement score is already queued or pending"
    if ticket is None or ticket.status != TicketStatus.SCORED:
        return "accepted score is not backed by a consumed validator ticket"
    return None


def _contract_retest_queue_gate(
    *,
    agent: Agent,
    target: Score | None,
    ticket: ValidatorTicket | None,
    replacement_open: bool,
) -> str | None:
    if agent.status not in _CONTRACT_RETEST_STATUSES:
        return "submission is not in a scoreable state"
    if target is None:
        return "validator has no accepted score to replace"
    if not _is_v9_contract_mismatch(target):
        return "accepted score already uses the authoritative v9 contract"
    if replacement_open:
        return "replacement score is already queued or pending"
    if ticket is None:
        return "accepted score has no validator ticket to reuse"
    return None


async def _finalized_quorum_state(
    session: AsyncSession,
) -> tuple[dict[UUID, list[Score]], dict[UUID, list[ValidatorTicket]], int]:
    """Load every finalized agent's scores and tickets in three statements.

    The per-agent :func:`_load` costs seven round trips: the agent row this
    scan already holds, its scores, its tickets, three to resolve a canonical
    version that cannot change inside one request, and a recovery list this
    route never reads. Multiplied by the whole finalized fleet that is
    thousands of sequential queries, and the scan outruns its caller's timeout
    long before it can answer. The row order matches ``_load`` exactly:
    ``_snapshot`` hashes tickets in the order it receives them, and the
    operator actions on this page replay that hash as their concurrency guard.

    ``Score.details`` is deferred because it is the other half of the cost.
    Outlier detection reads composites and the snapshot fields; the per-case
    audit breakdown is ~18KB a row, so carrying it fleet-wide is tens of
    megabytes of JSON parsed to reach a handful of floats. Nothing on this
    route touches it, and under the async session a future reader that does
    fails loudly rather than silently re-fetching.
    """
    canonical_version = await active_bench_version(session)
    finalized_ids = (
        select(Agent.agent_id)
        .where(Agent.status.in_(_FINALIZED_STATUSES))
        .scalar_subquery()
    )
    scores_by_agent: dict[UUID, list[Score]] = {}
    for score in (
        await session.scalars(
            select(Score)
            .where(Score.agent_id.in_(finalized_ids))
            .options(defer(Score.details))
            .order_by(Score.validator_hotkey.asc(), Score.run_id.asc())
        )
    ).all():
        scores_by_agent.setdefault(score.agent_id, []).append(score)
    tickets_by_agent: dict[UUID, list[ValidatorTicket]] = {}
    for ticket in (
        await session.scalars(
            select(ValidatorTicket)
            .where(ValidatorTicket.agent_id.in_(finalized_ids))
            .order_by(
                ValidatorTicket.deadline.asc(),
                ValidatorTicket.validator_hotkey.asc(),
            )
        )
    ).all():
        tickets_by_agent.setdefault(ticket.agent_id, []).append(ticket)
    return scores_by_agent, tickets_by_agent, canonical_version


@router.get("/score-outliers", response_model=AdminScoreOutlierList)
async def list_score_outliers(
    _admin: AdminDep,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminScoreOutlierList:
    """List active-era three-score quorums with one unambiguous extreme.

    Restricted to the benchmark era the platform is scoring now. The action
    this page offers is a re-test, and a re-test runs the current contract:
    a submission finalized under an older era cannot be re-scored into a
    number comparable with the one it holds, so listing it would only offer
    the operator a button that cannot honestly be pressed.
    """
    agents = list(
        (
            await session.scalars(
                select(Agent)
                .where(Agent.status.in_(_FINALIZED_STATUSES))
                .order_by(Agent.agent_id.asc())
            )
        ).all()
    )
    (
        scores_by_agent,
        tickets_by_agent,
        canonical_version,
    ) = await _finalized_quorum_state(session)
    detected: list[AdminScoreOutlier] = []
    lifecycle_cache: dict[str, dict[UUID, ScoreAuditEntry]] = {}
    position_cache: dict[str, dict[UUID, int]] = {}
    for agent in agents:
        all_scores = scores_by_agent.get(agent.agent_id, [])
        all_tickets = tickets_by_agent.get(agent.agent_id, [])
        bench_version = resolve_bench_version(
            all_tickets=all_tickets,
            all_scores=all_scores,
            canonical_version=canonical_version,
        )
        if bench_version != canonical_version:
            continue
        scores = [score for score in all_scores if score.bench_version == bench_version]
        tickets = [
            ticket for ticket in all_tickets if ticket.bench_version == bench_version
        ]
        result = _detect_outlier(scores)
        if result is None:
            continue
        target, direction, deviation, peer_spread = result
        ticket = next(
            (
                item
                for item in tickets
                if item.validator_hotkey == target.validator_hotkey
            ),
            None,
        )
        if target.validator_hotkey not in lifecycle_cache:
            lifecycle = await latest_retest_events_for_validator(
                session, validator_hotkey=target.validator_hotkey
            )
            lifecycle_cache[target.validator_hotkey] = lifecycle
            queued_entries = sorted(
                (
                    entry
                    for entry in lifecycle.values()
                    if entry.event == EVENT_SCORE_RETEST_QUEUED
                ),
                key=lambda entry: entry.seq,
            )
            position_cache[target.validator_hotkey] = {
                entry.agent_id: index
                for index, entry in enumerate(queued_entries, start=1)
            }
        latest = lifecycle_cache[target.validator_hotkey].get(agent.agent_id)
        pending = retest_is_active(latest)
        queued = retest_is_queued(latest)
        busy = await _validator_busy_elsewhere(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=target.validator_hotkey,
        )
        blocking = _replacement_gate(
            agent=agent,
            target=target,
            ticket=ticket,
            validator_busy=busy,
            replacement_open=retest_is_open(latest),
        )
        queue_blocking = _queue_gate(
            agent=agent,
            target=target,
            ticket=ticket,
            replacement_open=retest_is_open(latest),
        )
        queue_positions = position_cache[target.validator_hotkey]
        detected.append(
            AdminScoreOutlier(
                agent_id=agent.agent_id,
                agent_name=agent.name,
                miner_hotkey=agent.miner_hotkey,
                agent_status=agent.status.value,
                bench_version=bench_version,
                snapshot=_snapshot(agent=agent, scores=scores, tickets=tickets),
                median_composite=float(
                    statistics.median(score.composite for score in scores)
                ),
                direction=direction,  # type: ignore[arg-type]
                outlier=_outlier_score(target),
                peers=[
                    _outlier_score(score) for score in scores if score is not target
                ],
                deviation=deviation,
                peer_spread=peer_spread,
                ticket_status=ticket.status.value if ticket is not None else None,
                replacement_pending=pending,
                replacement_queued=queued,
                queue_position=queue_positions.get(agent.agent_id),
                replacement_deadline=(
                    ticket.deadline if pending and ticket is not None else None
                ),
                replacement_allowed=blocking is None,
                blocking_reason=blocking,
                queue_allowed=queue_blocking is None,
                queue_blocking_reason=queue_blocking,
            )
        )

    # Ranked before slicing: the page is a worklist ordered by how far the
    # outlier sits from its peers, so a stable full-set ordering is what makes
    # `offset` mean the same thing from one request to the next. `agent_id`
    # breaks ties, since equal deviations are otherwise ordered by nothing and
    # could swap between pages and hide a row.
    detected.sort(key=lambda item: (-item.deviation, str(item.agent_id)))
    return AdminScoreOutlierList(
        items=detected[offset : offset + limit],
        count=len(detected),
        limit=limit,
        offset=offset,
        bench_version=canonical_version,
    )


@router.get("/v9-contract-retests", response_model=AdminV9ContractRetestList)
async def list_v9_contract_retests(
    _admin: AdminDep,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminV9ContractRetestList:
    """Preview accepted v9 scores produced by a non-authoritative contract.

    This is deliberately independent of statistical disagreement and of the
    active benchmark version. During a rollout, an evaluating agent can hold a
    validly signed shadow score alongside unfinished v9 tickets; that exact
    score still needs a same-validator replacement before v9 may activate.
    """
    rows = list(
        (
            await session.execute(
                select(Agent, Score)
                .join(Score, Score.agent_id == Agent.agent_id)
                .where(
                    Agent.status.in_(_CONTRACT_RETEST_STATUSES),
                    Score.bench_version == 9,
                )
                .order_by(
                    Score.validator_hotkey.asc(),
                    Agent.agent_id.asc(),
                    Score.run_id.asc(),
                )
            )
        ).all()
    )
    candidates = [
        (agent, score) for agent, score in rows if _is_v9_contract_mismatch(score)
    ]
    agent_ids = {agent.agent_id for agent, _score in candidates}

    scores_by_agent: dict[UUID, list[Score]] = {}
    tickets_by_agent: dict[UUID, list[ValidatorTicket]] = {}
    if agent_ids:
        for score in (
            await session.scalars(
                select(Score)
                .where(Score.agent_id.in_(agent_ids), Score.bench_version == 9)
                .order_by(Score.validator_hotkey.asc(), Score.run_id.asc())
            )
        ).all():
            scores_by_agent.setdefault(score.agent_id, []).append(score)
        for ticket_row in (
            await session.scalars(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id.in_(agent_ids),
                    ValidatorTicket.bench_version == 9,
                )
                .order_by(
                    ValidatorTicket.deadline.asc(),
                    ValidatorTicket.validator_hotkey.asc(),
                )
            )
        ).all():
            tickets_by_agent.setdefault(ticket_row.agent_id, []).append(ticket_row)

    lifecycle_cache: dict[str, dict[UUID, ScoreAuditEntry]] = {}
    position_cache: dict[str, dict[UUID, int]] = {}
    items: list[AdminV9ContractRetestItem] = []
    for agent, target in candidates:
        hotkey = target.validator_hotkey
        if hotkey not in lifecycle_cache:
            lifecycle_cache[hotkey] = await latest_retest_events_for_validator(
                session, validator_hotkey=hotkey
            )
            position_cache[hotkey] = await score_retest_queue_positions(
                session, validator_hotkey=hotkey
            )
        scores = scores_by_agent.get(agent.agent_id, [])
        tickets = tickets_by_agent.get(agent.agent_id, [])
        target_ticket = next(
            (item for item in tickets if item.validator_hotkey == hotkey), None
        )
        latest = lifecycle_cache[hotkey].get(agent.agent_id)
        blocking = _contract_retest_queue_gate(
            agent=agent,
            target=target,
            ticket=target_ticket,
            replacement_open=retest_is_open(latest),
        )
        revision, manifest, rollout_mode, factor = _v9_contract_observation(target)
        items.append(
            AdminV9ContractRetestItem(
                agent_id=agent.agent_id,
                agent_name=agent.name,
                miner_hotkey=agent.miner_hotkey,
                agent_status=agent.status.value,
                validator_hotkey=hotkey,
                run_id=target.run_id,
                composite=target.composite,
                snapshot=_snapshot(agent=agent, scores=scores, tickets=tickets),
                observed_revision=revision,
                observed_manifest_sha256=manifest,
                observed_rollout_mode=rollout_mode,
                semantic_gate_factor_bps=factor,
                ticket_status=(
                    target_ticket.status.value if target_ticket is not None else None
                ),
                replacement_pending=retest_is_active(latest),
                replacement_queued=retest_is_queued(latest),
                queue_position=position_cache[hotkey].get(agent.agent_id),
                queue_allowed=blocking is None,
                queue_blocking_reason=blocking,
            )
        )

    return AdminV9ContractRetestList(
        items=items[offset : offset + limit],
        count=len(items),
        limit=limit,
        offset=offset,
        required_revision=V9_SCORE_CONTRACT_REVISION,
        required_manifest_sha256=V9_SCORE_CONTRACT_MANIFEST_SHA256,
    )


@router.get(
    "/validation-retries/{agent_id}/validators/{validator_hotkey}",
    response_model=AdminValidatorScoreReplacementDetail,
)
async def inspect_validator_score_replacement(
    agent_id: UUID,
    validator_hotkey: str,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminValidatorScoreReplacementDetail:
    agent, bench_version, scores, tickets, target = await _replacement_state(
        session,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        for_update=False,
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    ticket = next(
        (
            item
            for item in tickets
            if item.validator_hotkey == validator_hotkey
            and item.bench_version == bench_version
        ),
        None,
    )
    latest = await get_latest_score_retest_event(
        session, agent_id=agent_id, validator_hotkey=validator_hotkey
    )
    pending = retest_is_active(latest)
    reason = _replacement_gate(
        agent=agent,
        target=target,
        ticket=ticket,
        validator_busy=await _validator_busy_elsewhere(
            session, agent_id=agent_id, validator_hotkey=validator_hotkey
        ),
        replacement_open=retest_is_open(latest),
    )
    return AdminValidatorScoreReplacementDetail(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        agent_status=agent.status.value,
        bench_version=bench_version,
        score_count=len(scores),
        quorum=SCORING_QUORUM,
        snapshot=_snapshot(agent=agent, scores=scores, tickets=tickets),
        run_id=target.run_id if target is not None else None,
        composite=target.composite if target is not None else None,
        ticket_status=ticket.status.value if ticket is not None else None,
        ticket_deadline=ticket.deadline if ticket is not None else None,
        replacement_pending=pending,
        replacement_request_id=(
            UUID(str(latest.payload["request_id"]))
            if pending and latest is not None
            else None
        ),
        replacement_reason=(
            str(latest.payload["reason"]) if pending and latest is not None else None
        ),
        replacement_actor=(
            str(latest.payload["actor"]) if pending and latest is not None else None
        ),
        replacement_allowed=reason is None,
        blocking_reason=reason,
    )


@router.post(
    "/validation-retries/validators/{validator_hotkey}/queue-score-retests",
    response_model=AdminValidatorScoreRetestQueueResponse,
)
async def queue_validator_score_retests(
    validator_hotkey: str,
    payload: AdminValidatorScoreRetestQueueRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidatorScoreRetestQueueResponse:
    """Queue exact outliers or v9 contract mismatches behind current work.

    Every item is independently snapshot/run checked. Accepted scores stay
    canonical; only one request is promoted to an issued ticket at a time.
    """
    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    preliminary: dict[UUID, tuple[str, str | None]] = {}
    async with session.begin():
        for item in payload.items:
            lifecycle_entries = list(
                (
                    await session.scalars(
                        select(ScoreAuditEntry).where(
                            ScoreAuditEntry.agent_id == item.agent_id,
                            ScoreAuditEntry.validator_hotkey == validator_hotkey,
                            ScoreAuditEntry.event.in_(
                                (
                                    EVENT_SCORE_RETEST_QUEUED,
                                    EVENT_SCORE_RETEST_REQUESTED,
                                )
                            ),
                        )
                    )
                ).all()
            )
            prior = next(
                (
                    entry
                    for entry in lifecycle_entries
                    if entry.payload.get("request_id") == str(item.request_id)
                ),
                None,
            )
            if prior is not None:
                if (
                    prior.payload.get("actor") == actor
                    and prior.payload.get("reason") == payload.reason
                    and prior.payload.get("basis", "statistical_outlier")
                    == payload.basis
                    and prior.payload.get("run_id") == item.expected_run_id
                    and prior.payload.get("expected_snapshot") == item.expected_snapshot
                ):
                    preliminary[item.agent_id] = ("idempotent", None)
                else:
                    preliminary[item.agent_id] = (
                        "skipped",
                        "request id already used with different evidence",
                    )
                continue

            agent, bench_version, scores, tickets, target = await _replacement_state(
                session,
                agent_id=item.agent_id,
                validator_hotkey=validator_hotkey,
                for_update=True,
                required_bench_version=(
                    9 if payload.basis == "v9_contract_mismatch" else None
                ),
            )
            if agent is None:
                preliminary[item.agent_id] = ("skipped", "agent not found")
                continue
            current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
            if current_snapshot != item.expected_snapshot:
                preliminary[item.agent_id] = (
                    "skipped",
                    "validation state changed",
                )
                continue
            if target is not None and target.run_id != item.expected_run_id:
                preliminary[item.agent_id] = ("skipped", "accepted score run changed")
                continue
            if payload.basis == "statistical_outlier":
                detected = _detect_outlier(scores)
                if (
                    detected is None
                    or detected[0].validator_hotkey != validator_hotkey
                    or detected[0].run_id != item.expected_run_id
                ):
                    preliminary[item.agent_id] = (
                        "skipped",
                        "validator score is no longer the detected outlier",
                    )
                    continue
            elif (
                bench_version != 9
                or target is None
                or target.run_id != item.expected_run_id
                or not _is_v9_contract_mismatch(target)
            ):
                preliminary[item.agent_id] = (
                    "skipped",
                    "validator score no longer has a v9 contract mismatch",
                )
                continue
            ticket = next(
                (
                    candidate
                    for candidate in tickets
                    if candidate.validator_hotkey == validator_hotkey
                    and candidate.bench_version == bench_version
                ),
                None,
            )
            latest = await get_latest_score_retest_event(
                session,
                agent_id=item.agent_id,
                validator_hotkey=validator_hotkey,
            )
            gate = (
                _queue_gate
                if payload.basis == "statistical_outlier"
                else _contract_retest_queue_gate
            )
            reason = gate(
                agent=agent,
                target=target,
                ticket=ticket,
                replacement_open=retest_is_open(latest),
            )
            if reason is not None:
                preliminary[item.agent_id] = ("skipped", reason)
                continue
            assert target is not None
            assert ticket is not None
            audit_payload: dict[str, object] = {
                "request_id": str(item.request_id),
                "actor": actor,
                "reason": payload.reason,
                "expected_snapshot": item.expected_snapshot,
                "bench_version": bench_version,
                "run_id": item.expected_run_id,
                "preserved_score": _score_evidence(target),
                "preserved_score_count": len(scores),
                "queue_group_size": len(payload.items),
            }
            if payload.basis == "v9_contract_mismatch":
                audit_payload.update(
                    {
                        "basis": payload.basis,
                        "queued_ticket_status": ticket.status.value,
                        "required_v9_contract": {
                            "revision": V9_SCORE_CONTRACT_REVISION,
                            "manifest_sha256": V9_SCORE_CONTRACT_MANIFEST_SHA256,
                            "rollout_mode": "enforce",
                        },
                    }
                )
            await append_audit_entry(
                session,
                agent_id=item.agent_id,
                validator_hotkey=validator_hotkey,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload=audit_payload,
                recorded_at=now,
            )
            preliminary[item.agent_id] = ("queued", None)

        heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
        await activate_next_score_retest(
            session,
            validator_hotkey=validator_hotkey,
            now=now,
            supports_version=lambda version: (
                heartbeat is not None
                and heartbeat_supports_version(heartbeat, now=now, version=version)
            ),
        )
        latest_by_agent = await latest_retest_events_for_validator(
            session, validator_hotkey=validator_hotkey
        )
        positions = await score_retest_queue_positions(
            session, validator_hotkey=validator_hotkey
        )
        results: list[AdminValidatorScoreRetestQueueResult] = []
        for item in payload.items:
            status, detail = preliminary[item.agent_id]
            latest = latest_by_agent.get(item.agent_id)
            if (
                status == "queued"
                and latest is not None
                and latest.event == EVENT_SCORE_RETEST_REQUESTED
                and latest.payload.get("request_id") == str(item.request_id)
            ):
                status = "activated"
            results.append(
                AdminValidatorScoreRetestQueueResult(
                    agent_id=item.agent_id,
                    request_id=item.request_id,
                    status=status,  # type: ignore[arg-type]
                    detail=detail,
                    queue_position=positions.get(item.agent_id),
                )
            )

    return AdminValidatorScoreRetestQueueResponse(
        validator_hotkey=validator_hotkey,
        activated=sum(result.status == "activated" for result in results),
        queued=sum(result.status == "queued" for result in results),
        idempotent=sum(result.status == "idempotent" for result in results),
        skipped=sum(result.status == "skipped" for result in results),
        results=results,
    )


@router.post(
    "/validation-retries/{agent_id}/validators/{validator_hotkey}/replace-score",
    response_model=AdminValidatorScoreReplacementResponse,
)
async def replace_validator_score_after_infrastructure_failure(
    agent_id: UUID,
    validator_hotkey: str,
    payload: AdminValidatorScoreReplacementRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidatorScoreReplacementResponse:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    async with session.begin():
        prior_entries = list(
            (
                await session.scalars(
                    select(ScoreAuditEntry).where(
                        ScoreAuditEntry.agent_id == agent_id,
                        ScoreAuditEntry.validator_hotkey == validator_hotkey,
                        ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
                    )
                )
            ).all()
        )
        prior = next(
            (
                entry
                for entry in prior_entries
                if entry.payload.get("request_id") == str(payload.request_id)
            ),
            None,
        )
        if prior is not None:
            if (
                prior.payload.get("actor") != actor
                or prior.payload.get("reason") != payload.reason
                or prior.payload.get("run_id") != payload.expected_run_id
                or prior.payload.get("expected_snapshot") != payload.expected_snapshot
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            return AdminValidatorScoreReplacementResponse(
                request_id=payload.request_id,
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                original_run_id=payload.expected_run_id,
                bench_version=int(prior.payload["bench_version"]),
                replacement_deadline=datetime.fromisoformat(
                    str(prior.payload["replacement_deadline"])
                ),
                preserved_score_count=int(prior.payload["preserved_score_count"]),
                idempotent=True,
            )

        agent, bench_version, scores, tickets, target = await _replacement_state(
            session,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            for_update=True,
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
        # ``bench_version`` here comes from ``resolve_bench_version``, which
        # answers "what era was this agent last active in" -- so for an agent
        # whose last ticket or score was v6 it answers 6. This route then flips
        # that exact ticket back to ISSUED for another 90 minutes.
        #
        # It is an UPDATE, so it is not the INSERT path, and it had no era
        # check of any kind (unlike ``reinstatement_gate``, which refuses a
        # stale era by name). The database now refuses the re-lease outright;
        # this is here so an operator gets a 409 that says why, rather than a
        # 500 raised out of a trigger.
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"benchmark v{bench_version} is retired; a replacement "
                    "ticket cannot be issued for it"
                ),
            )
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(status_code=409, detail="validation state changed")
        if target is not None and target.run_id != payload.expected_run_id:
            raise HTTPException(status_code=409, detail="accepted score run changed")
        ticket = next(
            (
                item
                for item in tickets
                if item.validator_hotkey == validator_hotkey
                and item.bench_version == bench_version
            ),
            None,
        )
        reason = _replacement_gate(
            agent=agent,
            target=target,
            ticket=ticket,
            validator_busy=await _validator_busy_elsewhere(
                session, agent_id=agent_id, validator_hotkey=validator_hotkey
            ),
            replacement_open=False,
        )
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
        assert target is not None and ticket is not None
        now = datetime.now(UTC)
        deadline = now + REPLACEMENT_TICKET_TTL
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CANONICAL_QUORUM
        ticket.purpose_revision += 1
        ticket.legacy_completion_allowed = False
        ticket.issued_at = now
        ticket.deadline = deadline
        ticket.attempt_count += 1
        ticket.retry_after = None
        ticket.first_reported_at = None
        await append_audit_entry(
            session,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            event=EVENT_SCORE_RETEST_REQUESTED,
            payload={
                "request_id": str(payload.request_id),
                "actor": actor,
                "reason": payload.reason,
                "expected_snapshot": payload.expected_snapshot,
                "bench_version": bench_version,
                "run_id": payload.expected_run_id,
                "preserved_score": _score_evidence(target),
                "replacement_deadline": deadline.isoformat(),
                "preserved_score_count": len(scores),
            },
            recorded_at=now,
        )
        await session.flush()
    return AdminValidatorScoreReplacementResponse(
        request_id=payload.request_id,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        original_run_id=payload.expected_run_id,
        bench_version=bench_version,
        replacement_deadline=deadline,
        preserved_score_count=len(scores),
        idempotent=False,
    )


@router.post(
    "/validation-retries/{agent_id}/validators/{validator_hotkey}/release-ticket",
    response_model=AdminValidatorScoreRetestReleaseResponse,
)
async def release_validator_score_retest_ticket(
    agent_id: UUID,
    validator_hotkey: str,
    payload: AdminValidatorScoreRetestReleaseRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidatorScoreRetestReleaseResponse:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    async with session.begin():
        release_entries = list(
            (
                await session.scalars(
                    select(ScoreAuditEntry).where(
                        ScoreAuditEntry.agent_id == agent_id,
                        ScoreAuditEntry.validator_hotkey == validator_hotkey,
                        ScoreAuditEntry.event == EVENT_SCORE_RETEST_RELEASED,
                    )
                )
            ).all()
        )
        prior = next(
            (
                entry
                for entry in release_entries
                if entry.payload.get("request_id") == str(payload.request_id)
            ),
            None,
        )
        if prior is not None:
            if (
                prior.payload.get("actor") != actor
                or prior.payload.get("reason") != payload.reason
                or prior.payload.get("expected_snapshot") != payload.expected_snapshot
                or prior.payload.get("expected_deadline")
                != _aware(payload.expected_deadline).isoformat()
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            return AdminValidatorScoreRetestReleaseResponse(
                request_id=payload.request_id,
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                status="scored",
                preserved_run_id=str(prior.payload["preserved_run_id"]),
                idempotent=True,
            )

        agent, bench_version, scores, tickets, target = await _replacement_state(
            session,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            for_update=True,
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(status_code=409, detail="validation state changed")
        latest = await get_latest_score_retest_event(
            session, agent_id=agent_id, validator_hotkey=validator_hotkey
        )
        if latest is None or latest.event != EVENT_SCORE_RETEST_REQUESTED:
            raise HTTPException(
                status_code=409, detail="no replacement ticket is pending"
            )
        ticket = next(
            (
                item
                for item in tickets
                if item.validator_hotkey == validator_hotkey
                and item.bench_version == bench_version
            ),
            None,
        )
        if target is None or ticket is None:
            raise HTTPException(
                status_code=409, detail="replacement state is incomplete"
            )
        if _aware(ticket.deadline) != _aware(payload.expected_deadline):
            raise HTTPException(status_code=409, detail="replacement ticket changed")
        if ticket.status not in {TicketStatus.ISSUED, TicketStatus.EXPIRED}:
            raise HTTPException(
                status_code=409, detail="replacement ticket is not releasable"
            )
        ticket.status = TicketStatus.SCORED
        ticket.retry_after = None
        now = datetime.now(UTC)
        await append_audit_entry(
            session,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            event=EVENT_SCORE_RETEST_RELEASED,
            payload={
                "request_id": str(payload.request_id),
                "retest_request_id": latest.payload.get("request_id"),
                "actor": actor,
                "reason": payload.reason,
                "expected_snapshot": payload.expected_snapshot,
                "expected_deadline": _aware(payload.expected_deadline).isoformat(),
                "bench_version": bench_version,
                "preserved_run_id": target.run_id,
            },
            recorded_at=now,
        )
        heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
        await activate_next_score_retest(
            session,
            validator_hotkey=validator_hotkey,
            now=now,
            supports_version=lambda version: (
                heartbeat is not None
                and heartbeat_supports_version(heartbeat, now=now, version=version)
            ),
        )
        await session.flush()
    return AdminValidatorScoreRetestReleaseResponse(
        request_id=payload.request_id,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        status="scored",
        preserved_run_id=target.run_id,
        idempotent=False,
    )

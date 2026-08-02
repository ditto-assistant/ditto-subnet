"""Audited close-out for submissions whose benchmark generation has ended.

A rollout is forward-only. When a newer benchmark version activates, the older
one stops being scored, and every submission still waiting for a ticket in the
closed era becomes unfinishable. Nothing in the data model said so, so those
rows kept sitting in the miner-facing "Waiting for scores" column holding a
queue rank they could never consume.

This module gives them a terminal state. It changes no scoring semantics: the
agent row is not mutated at all, no score is created or removed, and the
submission stays fully visible and searchable. All it writes is one append-only
:class:`~ditto.db.models.SubmissionRetirement` row that the queue projections
already know how to respect.

The snapshot helpers are imported from
:mod:`ditto.api_server.endpoints.admin_validation_retry` rather than
reimplemented. There must be exactly one definition of "the state this
submission was in when the operator looked at it", so an operator can read a
snapshot from either triage route and hand it to either action.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_retirement import (
    AdminRetirementBatchRequest,
    AdminRetirementBatchResponse,
    AdminRetirementBatchResult,
    AdminRetirementCandidate,
    AdminRetirementPreviewResponse,
    AdminRetirementRequest,
    AdminRetirementResponse,
    AdminSubmissionRetirement,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_validation_retry import (
    _load,
    _require_actor,
    _snapshot,
    _ticket_wire,
)
from ditto.db.models import SubmissionRetirement
from ditto.db.queries.agents import list_agents_by_status
from ditto.db.queries.benchmark_admission import admitted_agent_ids
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.retirement import (
    finalized_prev_gen_count,
    load_retirement,
    load_withdrawal_versions,
    retirement_count,
    retirement_gate,
)
from ditto.db.queries.retry_state import classify_agent_retry_states
from ditto.db.queries.scores import SCORING_QUORUM

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Matches the stuck-submission sweep: the evaluating backlog is bounded, and
# this caps a pathological scan rather than paginating a triage view.
_RETIREMENT_SCAN_LIMIT = 2000


def _retirement_item(row: SubmissionRetirement) -> AdminSubmissionRetirement:
    return AdminSubmissionRetirement(
        retirement_id=row.retirement_id,
        agent_id=row.agent_id,
        bench_version=row.bench_version,
        superseded_by_version=row.superseded_by_version,
        actor=row.actor,
        reason=row.reason,
        expected_snapshot=row.expected_snapshot,
        score_count=row.score_count,
        created_at=row.created_at,
    )


async def _agent_is_admitted(
    session: AsyncSession, *, agent_id: UUID, active_version: int
) -> bool:
    return agent_id in await admitted_agent_ids(
        session, bench_version=active_version, agent_ids=[agent_id]
    )


@router.get("/retirements", response_model=AdminRetirementPreviewResponse)
async def preview_retirements(
    _admin: AdminDep,
    session: SessionDep,
    population: Annotated[list[str] | None, Query()] = None,
) -> AdminRetirementPreviewResponse:
    """Which submissions a retirement would close out, grouped honestly.

    Read this before acting. "Previous generation" is three different groups
    and only two of them are ever eligible; ``population_counts`` and
    ``finalized`` in the response make the split explicit so a broad instruction
    does not silently retire work someone is still owed.
    """
    now = datetime.now(UTC)
    requested = set(population or [])
    known = {"never_scored", "partially_scored", "finalized"}
    unknown = requested - known
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="unknown population: " + ", ".join(sorted(unknown)),
        )

    active_version = await active_bench_version(session)
    agents = await list_agents_by_status(
        session, statuses=[AgentStatus.EVALUATING], limit=_RETIREMENT_SCAN_LIMIT
    )
    # ``classify_agent_retry_states`` already drops withdrawn and retired rows
    # and everything at or above quorum, so what survives is exactly the
    # below-quorum waiting population. Retired rows are counted separately.
    classified = await classify_agent_retry_states(session, agents=agents, now=now)
    agents_by_id = {agent.agent_id: agent for agent in agents}
    admitted = await admitted_agent_ids(
        session,
        bench_version=active_version,
        agent_ids=list(classified.keys()),
    )

    candidates: list[AdminRetirementCandidate] = []
    population_counts: dict[str, int] = {}
    bench_version_counts: dict[str, int] = {}
    blocked = 0
    for agent_id, retry in classified.items():
        agent = agents_by_id[agent_id]
        verdict = retirement_gate(
            agent=agent,
            scores=retry.scores,
            tickets=retry.tickets,
            bench_version=retry.bench_version,
            active_version=active_version,
            admitted_to_active_era=agent_id in admitted,
            # Both are structurally impossible for anything that survived
            # ``classify_agent_retry_states``: it skips withdrawn and retired
            # (agent, era) pairs outright. Passing False here avoids an N+1
            # sweep; the apply path re-checks both under a row lock, which is
            # where the check actually has to be authoritative.
            already_retired=False,
            already_withdrawn=False,
        )
        if not verdict.allowed:
            # Current-generation work is the overwhelming majority here and is
            # not interesting to an operator running this preview. Only report
            # a block for a row whose generation has actually closed.
            if retry.bench_version < active_version:
                blocked += 1
            else:
                continue
        if requested and verdict.population not in requested:
            continue
        if verdict.allowed:
            population_counts[verdict.population] = (
                population_counts.get(verdict.population, 0) + 1
            )
            key = f"v{retry.bench_version}"
            bench_version_counts[key] = bench_version_counts.get(key, 0) + 1
        candidates.append(
            AdminRetirementCandidate(
                agent_id=agent_id,
                miner_hotkey=agent.miner_hotkey,
                agent_name=agent.name,
                agent_version=agent.version,
                agent_status=agent.status.value,
                bench_version=retry.bench_version,
                active_bench_version=active_version,
                score_count=retry.score_count,
                quorum=SCORING_QUORUM,
                population=verdict.population,
                attempts_used=max((t.attempt_count for t in retry.tickets), default=0),
                snapshot=_snapshot(
                    agent=agent, scores=retry.scores, tickets=retry.tickets
                ),
                retirement_allowed=verdict.allowed,
                blocking_reason=verdict.reason,
                retirement=None,
                submitted_at=agent.created_at,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.bench_version,
            -item.score_count,
            item.submitted_at,
            item.agent_id,
        )
    )
    return AdminRetirementPreviewResponse(
        generated_at=now,
        active_bench_version=active_version,
        quorum=SCORING_QUORUM,
        eligible_count=sum(1 for item in candidates if item.retirement_allowed),
        already_retired_count=await retirement_count(session),
        blocked_count=blocked,
        finalized_prev_gen_count=await finalized_prev_gen_count(
            session, active_version=active_version
        ),
        population_counts=population_counts,
        bench_version_counts=bench_version_counts,
        candidates=candidates,
    )


async def _apply_retirement(
    session: AsyncSession,
    *,
    agent_id: UUID,
    actor: str,
    reason: str,
    request_id: UUID,
    expected_snapshot: str,
    now: datetime,
) -> tuple[str, str | None, SubmissionRetirement | None]:
    """Retire one submission inside the caller's transaction.

    Returns ``(status, detail, retirement)`` and never raises for an expected
    condition, so the batch route can collect a per-agent verdict instead of
    aborting the whole set. ``status`` is ``retired`` | ``idempotent`` |
    ``skipped``; ``detail`` explains a skip.
    """
    agent, bench_version, scores, tickets, _recoveries = await _load(
        session, agent_id=agent_id, for_update=True
    )
    if agent is None:
        return "skipped", "agent not found", None

    existing_request = await session.get(SubmissionRetirement, request_id)
    if existing_request is not None:
        if (
            existing_request.agent_id != agent_id
            or existing_request.actor != actor
            or existing_request.reason != reason
            or existing_request.expected_snapshot != expected_snapshot
        ):
            return "skipped", "request id already used", None
        return "idempotent", None, existing_request

    active_version = await active_bench_version(session)
    existing = await load_retirement(
        session, agent_id=agent_id, bench_version=bench_version, for_update=True
    )
    withdrawn_versions = await load_withdrawal_versions(session, agent_id=agent_id)
    admitted = await _agent_is_admitted(
        session, agent_id=agent_id, active_version=active_version
    )

    current_snapshot = _snapshot(agent=agent, scores=scores, tickets=tickets)
    if current_snapshot != expected_snapshot:
        return "skipped", "validation state changed", None

    verdict = retirement_gate(
        agent=agent,
        scores=scores,
        tickets=tickets,
        bench_version=bench_version,
        active_version=active_version,
        admitted_to_active_era=admitted,
        already_retired=existing is not None,
        already_withdrawn=bench_version in withdrawn_versions,
    )
    if not verdict.allowed:
        return "skipped", verdict.reason or "retirement unavailable", None

    retirement = SubmissionRetirement(
        retirement_id=request_id,
        agent_id=agent_id,
        bench_version=bench_version,
        superseded_by_version=active_version,
        actor=actor,
        reason=reason,
        expected_snapshot=current_snapshot,
        score_count=len(scores),
        ticket_snapshot=[_ticket_wire(ticket) for ticket in tickets],
        created_at=now,
    )
    session.add(retirement)
    await session.flush()
    return "retired", None, retirement


# Declared before the ``/{agent_id}`` route on purpose: FastAPI matches paths in
# declaration order, so a literal segment that could also parse as a path
# parameter has to come first or it is unreachable.
@router.post("/retirements/batch", response_model=AdminRetirementBatchResponse)
async def batch_retire_previous_generation_submissions(
    payload: AdminRetirementBatchRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminRetirementBatchResponse:
    """Retire several closed-generation submissions in one operator action.

    Each item is gated and snapshot-checked exactly like the single-agent
    route; an item whose state moved (or that no longer qualifies) is skipped
    with a reason rather than force-applied. All retirements commit together.
    """
    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    results: list[AdminRetirementBatchResult] = []
    async with session.begin():
        for item in payload.items:
            status, detail, retirement = await _apply_retirement(
                session,
                agent_id=item.agent_id,
                actor=actor,
                reason=payload.reason,
                request_id=item.request_id,
                expected_snapshot=item.expected_snapshot,
                now=now,
            )
            results.append(
                AdminRetirementBatchResult(
                    agent_id=item.agent_id,
                    status=status,  # type: ignore[arg-type]
                    detail=detail,
                    retirement=(
                        _retirement_item(retirement) if retirement is not None else None
                    ),
                )
            )
    return AdminRetirementBatchResponse(
        retired=sum(1 for result in results if result.status == "retired"),
        idempotent=sum(1 for result in results if result.status == "idempotent"),
        skipped=sum(1 for result in results if result.status == "skipped"),
        results=results,
    )


@router.post("/retirements/{agent_id}", response_model=AdminRetirementResponse)
async def retire_previous_generation_submission(
    agent_id: UUID,
    payload: AdminRetirementRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminRetirementResponse:
    """Close out one submission whose benchmark generation has ended."""
    actor = _require_actor(x_admin_actor)
    now = datetime.now(UTC)
    async with session.begin():
        status, detail, retirement = await _apply_retirement(
            session,
            agent_id=agent_id,
            actor=actor,
            reason=payload.reason,
            request_id=payload.request_id,
            expected_snapshot=payload.expected_snapshot,
            now=now,
        )
        if status == "skipped":
            code = 404 if detail == "agent not found" else 409
            raise HTTPException(
                status_code=code, detail=detail or "retirement unavailable"
            )
        assert retirement is not None
        return AdminRetirementResponse(
            retirement=_retirement_item(retirement),
            idempotent=status == "idempotent",
        )


__all__ = ["router"]

"""Admin control and visibility for shadow core qualification."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.core_qualification import (
    AdminCoreQualificationPolicyRequest,
    AdminCoreQualificationPolicyResponse,
    AdminCoreQualificationRefreshRequest,
    AgentCoreQualificationStatus,
    CoreQualificationDecision,
)
from ditto.api_models.core_qualification import (
    CoreQualificationObservation as CoreQualificationObservationView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent, CoreQualificationObservation
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    latest_core_qualification_observation,
    latest_core_qualification_policy,
    list_agent_core_qualification_observations,
    list_core_qualification_policies,
    observe_core_qualification,
    policy_revision_from_row,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _confirmation(bench_version: int) -> str:
    return f"APPLY SHADOW CORE QUALIFICATION V{bench_version}"


async def _policy_response(
    session: AsyncSession,
    *,
    bench_version: int,
    history_limit: int,
) -> AdminCoreQualificationPolicyResponse:
    current = await latest_core_qualification_policy(
        session, bench_version=bench_version
    )
    history = await list_core_qualification_policies(
        session,
        bench_version=bench_version,
        limit=history_limit,
    )
    return AdminCoreQualificationPolicyResponse(
        bench_version=bench_version,
        configured=current is not None,
        current=policy_revision_from_row(current) if current is not None else None,
        history=[policy_revision_from_row(row) for row in history],
        required_confirmation=_confirmation(bench_version),
        shadow_only=True,
    )


@router.get(
    "/core-qualification/policy",
    response_model=AdminCoreQualificationPolicyResponse,
)
async def get_core_qualification_policy(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int, Query(ge=7)],
    history_limit: Annotated[int, Query(ge=0, le=200)] = 50,
) -> AdminCoreQualificationPolicyResponse:
    response.headers["Cache-Control"] = "no-store"
    return await _policy_response(
        session,
        bench_version=bench_version,
        history_limit=history_limit,
    )


@router.post(
    "/core-qualification/policy",
    response_model=AdminCoreQualificationPolicyResponse,
)
async def set_core_qualification_policy(
    payload: AdminCoreQualificationPolicyRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCoreQualificationPolicyResponse:
    response.headers["Cache-Control"] = "no-store"
    bench_version = payload.policy.bench_version
    if payload.confirmation != _confirmation(bench_version):
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{_confirmation(bench_version)}"',
        )
    try:
        async with session.begin():
            current = await latest_core_qualification_policy(
                session,
                bench_version=bench_version,
                for_update=True,
            )
            current_revision = current.revision if current is not None else 0
            if payload.expected_revision != current_revision:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "core qualification policy changed; re-read it and submit "
                        f"expected_revision={current_revision}"
                    ),
                )
            await insert_core_qualification_policy(
                session,
                parent_revision=current_revision,
                policy=payload.policy,
                reason=payload.reason,
                actor=payload.actor,
            )
    except SAIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="core qualification policy changed concurrently; re-read it",
        ) from error
    return await _policy_response(
        session,
        bench_version=bench_version,
        history_limit=50,
    )


def _stale_reason(
    row: CoreQualificationObservation,
    *,
    agent: Agent,
    bench_version: int,
    policy_revision: int | None,
) -> Literal[
    "current",
    "artifact_changed",
    "screened_image_changed",
    "benchmark_changed",
    "policy_changed",
]:
    if row.artifact_sha256 != agent.sha256:
        return "artifact_changed"
    if row.screened_image_sha256 != agent.screened_image_sha256:
        return "screened_image_changed"
    if row.bench_version != bench_version:
        return "benchmark_changed"
    if policy_revision is None or row.policy_revision != policy_revision:
        return "policy_changed"
    return "current"


def _observation_view(
    row: CoreQualificationObservation,
    *,
    stale_reason: Literal[
        "current",
        "artifact_changed",
        "screened_image_changed",
        "benchmark_changed",
        "policy_changed",
    ],
) -> CoreQualificationObservationView:
    evidence = row.score_evidence if isinstance(row.score_evidence, dict) else {}
    score_rows = evidence.get("scores", [])
    return CoreQualificationObservationView(
        sequence=row.sequence,
        observation_id=row.observation_id,
        agent_id=row.agent_id,
        artifact_sha256=row.artifact_sha256,
        screened_image_sha256=row.screened_image_sha256,
        bench_version=row.bench_version,
        policy_revision=row.policy_revision,
        policy_checksum=row.policy_checksum,
        score_evidence_sha256=row.score_evidence_sha256,
        score_count=row.score_count,
        full_size=row.full_size,
        complete_wave=row.complete_wave,
        validator_hotkeys=[
            str(item.get("validator_hotkey", "")) for item in score_rows
        ],
        run_ids=[str(item.get("run_id", "")) for item in score_rows],
        median_composite=row.median_composite,
        median_tool_mean=row.median_tool_mean,
        median_memory_mean=row.median_memory_mean,
        entry_passed=row.entry_passed,
        retention_passed=row.retention_passed,
        qualified=row.qualified,
        enter_streak=row.enter_streak,
        exit_streak=row.exit_streak,
        decision=cast(CoreQualificationDecision, row.decision),
        source=cast(Literal["score_commit", "admin_refresh"], row.source),
        actor=row.actor,
        reason=row.reason,
        observed_at=row.observed_at,
        weight_eligible=False,
        current=stale_reason == "current",
        stale_reason=stale_reason,
    )


async def _agent_status(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    limit: int,
) -> AgentCoreQualificationStatus:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    policy = await latest_core_qualification_policy(
        session,
        bench_version=bench_version,
    )
    rows, total = await list_agent_core_qualification_observations(
        session,
        agent_id=agent_id,
        limit=limit,
    )
    policy_revision = policy.revision if policy is not None else None
    observations = [
        _observation_view(
            row,
            stale_reason=_stale_reason(
                row,
                agent=agent,
                bench_version=bench_version,
                policy_revision=policy_revision,
            ),
        )
        for row in rows
    ]
    current_row = None
    if policy is not None and agent.screened_image_sha256 is not None:
        current_row = await latest_core_qualification_observation(
            session,
            agent_id=agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256,
            bench_version=bench_version,
            policy_revision=policy.revision,
        )
    current = (
        _observation_view(current_row, stale_reason="current")
        if current_row is not None
        else None
    )
    return AgentCoreQualificationStatus(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        miner_hotkey=agent.miner_hotkey,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        configured=policy is not None,
        qualified=current.qualified if current is not None else False,
        current_observation=current,
        total=total,
        observations=observations,
        shadow_only=True,
    )


@router.get(
    "/agents/{agent_id}/core-qualification",
    response_model=AgentCoreQualificationStatus,
)
async def get_agent_core_qualification(
    agent_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int, Query(ge=7)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AgentCoreQualificationStatus:
    response.headers["Cache-Control"] = "no-store"
    return await _agent_status(
        session,
        agent_id=agent_id,
        bench_version=bench_version,
        limit=limit,
    )


@router.post(
    "/agents/{agent_id}/core-qualification/refresh",
    response_model=AgentCoreQualificationStatus,
)
async def refresh_agent_core_qualification(
    agent_id: UUID,
    payload: AdminCoreQualificationRefreshRequest,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AgentCoreQualificationStatus:
    """Idempotently backfill or recover one current score snapshot."""

    response.headers["Cache-Control"] = "no-store"
    expected_confirmation = (
        f"REFRESH SHADOW CORE QUALIFICATION V{payload.bench_version}"
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f'confirmation must equal "{expected_confirmation}"',
        )
    async with session.begin():
        if await session.get(Agent, agent_id) is None:
            raise HTTPException(status_code=404, detail="agent not found")
        result = await observe_core_qualification(
            session,
            agent_id=agent_id,
            bench_version=payload.bench_version,
            now=datetime.now(UTC),
            source="admin_refresh",
            actor=payload.actor,
            reason=payload.reason,
        )
        if result is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no configured policy, screened image, or quorum score snapshot "
                    "is available for shadow core qualification"
                ),
            )
    return await _agent_status(
        session,
        agent_id=agent_id,
        bench_version=payload.bench_version,
        limit=50,
    )

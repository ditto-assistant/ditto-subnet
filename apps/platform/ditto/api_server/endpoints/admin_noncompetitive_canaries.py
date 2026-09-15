"""Audited reservation and binding of noncompetitive team canaries.

These endpoints can only remove an exact identity from competition. They never
change agent status, screening, copy detection or any other gate, and there is
no endpoint that lifts an exclusion.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_noncompetitive_canaries import (
    AdminTeamCanaryAgent,
    AdminTeamCanaryBindRequest,
    AdminTeamCanaryExclusion,
    AdminTeamCanaryList,
    AdminTeamCanaryReserveRequest,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.noncompetitive_exclusions import (
    NoncompetitiveExclusionError,
    NoncompetitiveExclusionNotFoundError,
    TeamCanaryAgent,
    TeamCanaryExclusion,
    bind_team_canary,
    count_team_canary_exclusions,
    list_team_canary_agents,
    list_team_canary_exclusions,
    reserve_team_canary,
)

router = APIRouter(prefix="/admin/noncompetitive-canaries", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _actor(header: str | None) -> str:
    actor = header.strip() if header else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


def _agent(agent: TeamCanaryAgent) -> AdminTeamCanaryAgent:
    return AdminTeamCanaryAgent.model_validate(
        {
            "agent_id": agent.agent_id,
            "status": agent.status,
            "sha256": agent.sha256,
            "screened_image_sha256": agent.screened_image_sha256,
            "state": agent.state,
        }
    )


def _exclusion(
    exclusion: TeamCanaryExclusion, agents: list[TeamCanaryAgent]
) -> AdminTeamCanaryExclusion:
    return AdminTeamCanaryExclusion(
        exclusion_id=exclusion.exclusion_id,
        miner_hotkey=exclusion.miner_hotkey,
        artifact_sha256=exclusion.artifact_sha256,
        reason=exclusion.reason,
        created_by=exclusion.created_by,
        created_at=exclusion.created_at,
        agent_id=exclusion.agent_id,
        screened_image_sha256=exclusion.screened_image_sha256,
        bound_by=exclusion.bound_by,
        bound_reason=exclusion.bound_reason,
        bound_at=exclusion.bound_at,
        matched_agents=[_agent(agent) for agent in agents],
    )


async def _single(
    session: AsyncSession, exclusion: TeamCanaryExclusion
) -> AdminTeamCanaryExclusion:
    agents = await list_team_canary_agents(session, [exclusion])
    return _exclusion(exclusion, agents[exclusion.exclusion_id])


@router.get("", response_model=AdminTeamCanaryList)
async def team_canaries(
    _admin: AdminDep,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminTeamCanaryList:
    """List audited team canaries, oldest first, with the agents each excludes."""
    total = await count_team_canary_exclusions(session)
    exclusions = await list_team_canary_exclusions(session, limit=limit, offset=offset)
    agents = await list_team_canary_agents(session, exclusions)
    return AdminTeamCanaryList(
        total=total,
        exclusions=[
            _exclusion(exclusion, agents[exclusion.exclusion_id])
            for exclusion in exclusions
        ],
    )


@router.post("", response_model=AdminTeamCanaryExclusion, status_code=201)
async def reserve(
    body: AdminTeamCanaryReserveRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminTeamCanaryExclusion:
    """Reserve one exact hotkey + artifact as a noncompetitive team canary."""
    actor = _actor(x_admin_actor)
    expected = f"RESERVE TEAM CANARY {body.miner_hotkey} {body.artifact_sha256}"
    if body.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    try:
        async with session.begin():
            exclusion = await reserve_team_canary(
                session,
                miner_hotkey=body.miner_hotkey,
                artifact_sha256=body.artifact_sha256,
                reason=body.reason,
                actor=actor,
            )
    except NoncompetitiveExclusionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return await _single(session, exclusion)


@router.post("/{exclusion_id}/bind", response_model=AdminTeamCanaryExclusion)
async def bind(
    exclusion_id: UUID,
    body: AdminTeamCanaryBindRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminTeamCanaryExclusion:
    """Bind a reservation once to the exact agent and screened image digest."""
    actor = _actor(x_admin_actor)
    expected = f"BIND TEAM CANARY {exclusion_id} {body.agent_id}"
    if body.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    try:
        async with session.begin():
            exclusion = await bind_team_canary(
                session,
                exclusion_id=exclusion_id,
                agent_id=body.agent_id,
                miner_hotkey=body.miner_hotkey,
                artifact_sha256=body.artifact_sha256,
                screened_image_sha256=body.screened_image_sha256,
                reason=body.reason,
                actor=actor,
            )
    except NoncompetitiveExclusionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except NoncompetitiveExclusionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return await _single(session, exclusion)

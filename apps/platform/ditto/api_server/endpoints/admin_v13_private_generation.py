"""Authenticated read-only compatibility for historical private-audit records.

Private screening-audit activation is retired. These digest-only views retain
access to immutable audit history without creating groups or packages.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.v13_private_generation import (
    V13GenerationGroupView,
    V13GroupPackageView,
    V13KnownBenignApprovalView,
    V13ReplayGenerationGroupView,
    V13ReplayGroupPackageView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    V13GroupPackageRegistration,
    V13KnownBenignControlApproval,
    V13PrivateGenerationGroup,
    V13ReplayGroupPackageRegistration,
    V13ReplayPrivateGenerationGroup,
)

router = APIRouter(prefix="/admin/v13-private-generation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _approval_view(row: V13KnownBenignControlApproval) -> V13KnownBenignApprovalView:
    return V13KnownBenignApprovalView.model_validate(
        {
            key: getattr(row, key)
            for key in V13KnownBenignApprovalView.model_fields
            if key != "status"
        }
    )


@router.get("/known-benign-approvals", response_model=list[V13KnownBenignApprovalView])
async def list_known_benign_approvals(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[V13KnownBenignApprovalView]:
    """List digest-only immutable approvals without private bank handles."""
    rows = (
        await session.scalars(
            select(V13KnownBenignControlApproval)
            .order_by(
                V13KnownBenignControlApproval.approved_at.desc(),
                V13KnownBenignControlApproval.approval_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_approval_view(row) for row in rows]


@router.get(
    "/known-benign-approvals/{approval_id}",
    response_model=V13KnownBenignApprovalView,
)
async def get_known_benign_approval(
    approval_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13KnownBenignApprovalView:
    row = await session.get(V13KnownBenignControlApproval, approval_id)
    if row is None:
        raise HTTPException(404, "known-benign approval not found")
    return _approval_view(row)


def _group_view(row: V13PrivateGenerationGroup) -> V13GenerationGroupView:
    return V13GenerationGroupView.model_validate(
        {
            key: getattr(row, key)
            for key in V13GenerationGroupView.model_fields
            if key != "status"
        }
    )


@router.get("/groups/{group_id}", response_model=V13GenerationGroupView)
async def get_generation_group(
    group_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13GenerationGroupView:
    """Read one digest-only generation start; never expose protected cases."""
    row = await session.get(V13PrivateGenerationGroup, group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="generation group not found")
    return _group_view(row)


def _package_view(
    group: V13PrivateGenerationGroup, row: V13GroupPackageRegistration
) -> V13GroupPackageView:
    if row.role not in {"target", "known_benign"}:
        raise HTTPException(status_code=503, detail="package role invalid")
    target = row.role == "target"
    return V13GroupPackageView(
        group_id=group.group_id,
        role=cast(Literal["target", "known_benign"], row.role),
        agent_id=group.target_agent_id if target else group.control_agent_id,
        attempt_id=group.target_attempt_id if target else group.control_attempt_id,
        artifact_sha256=(
            group.target_artifact_sha256 if target else group.control_artifact_sha256
        ),
        image_sha256=group.target_image_sha256
        if target
        else group.control_image_sha256,
        profile_sha256=group.profile_sha256,
        generation_receipt_sha256=row.generation_receipt_sha256,
        manifest_sha256=row.manifest_sha256,
        pair_inventory_sha256=row.pair_inventory_sha256,
        registrar_actor=row.registrar_actor,
        registered_at=row.registered_at,
    )


@router.get("/groups/{group_id}/packages/{role}", response_model=V13GroupPackageView)
async def get_group_package(
    group_id: UUID,
    role: Literal["target", "known_benign"],
    _admin: AdminDep,
    session: SessionDep,
) -> V13GroupPackageView:
    """Read a group role's exact metadata; legacy attempt rows never qualify."""
    group = await session.get(V13PrivateGenerationGroup, group_id)
    row = await session.get(V13GroupPackageRegistration, (group_id, role))
    if group is None or row is None:
        raise HTTPException(status_code=404, detail="group package not found")
    return _package_view(group, row)


def _replay_group_view(
    row: V13ReplayPrivateGenerationGroup,
) -> V13ReplayGenerationGroupView:
    return V13ReplayGenerationGroupView.model_validate(
        {
            key: getattr(row, key)
            for key in V13ReplayGenerationGroupView.model_fields
            if key != "status"
        }
    )


def _replay_package_view(
    group: V13ReplayPrivateGenerationGroup,
    row: V13ReplayGroupPackageRegistration,
) -> V13ReplayGroupPackageView:
    target = row.role == "target"
    return V13ReplayGroupPackageView(
        replay_id=group.replay_id,
        group_id=group.group_id,
        role=cast(Literal["target", "known_benign"], row.role),
        agent_id=group.target_agent_id if target else group.control_agent_id,
        attempt_id=group.target_attempt_id if target else group.control_attempt_id,
        artifact_sha256=(
            group.target_artifact_sha256 if target else group.control_artifact_sha256
        ),
        image_sha256=(
            group.target_image_sha256 if target else group.control_image_sha256
        ),
        profile_sha256=group.profile_sha256,
        generation_receipt_sha256=row.generation_receipt_sha256,
        manifest_sha256=row.manifest_sha256,
        pair_inventory_sha256=row.pair_inventory_sha256,
        registrar_actor=row.registrar_actor,
        registered_at=row.registered_at,
    )


@router.get("/replays/{replay_id}/group", response_model=V13ReplayGenerationGroupView)
async def get_replay_generation_group(
    replay_id: UUID, _admin: AdminDep, session: SessionDep
) -> V13ReplayGenerationGroupView:
    row = await session.scalar(
        select(V13ReplayPrivateGenerationGroup).where(
            V13ReplayPrivateGenerationGroup.replay_id == replay_id
        )
    )
    if row is None:
        raise HTTPException(404, "replay generation group not found")
    return _replay_group_view(row)


@router.get(
    "/replays/{replay_id}/packages/{role}",
    response_model=V13ReplayGroupPackageView,
)
async def get_replay_group_package(
    replay_id: UUID,
    role: Literal["target", "known_benign"],
    _admin: AdminDep,
    session: SessionDep,
) -> V13ReplayGroupPackageView:
    group = await session.scalar(
        select(V13ReplayPrivateGenerationGroup).where(
            V13ReplayPrivateGenerationGroup.replay_id == replay_id
        )
    )
    if group is None:
        raise HTTPException(404, "replay generation group not found")
    row = await session.get(V13ReplayGroupPackageRegistration, (group.group_id, role))
    if row is None:
        raise HTTPException(404, "replay group package not found")
    return _replay_package_view(group, row)

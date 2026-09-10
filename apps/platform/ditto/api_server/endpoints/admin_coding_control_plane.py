"""Admin-only redacted status for Coding release and launch operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_control_plane import (
    AdminCodingControlPlaneResponse,
    CodingHostedOperationRecord,
    CodingHostedOperationState,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import CodingHostedAssignment, CodingHostedPrivateTask

router = APIRouter(prefix="/admin/coding-control-plane", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _state(
    assignment: CodingHostedAssignment,
    task: CodingHostedPrivateTask | None,
    *,
    now: datetime,
) -> CodingHostedOperationState:
    if task is not None and task.closed_at is not None:
        assert task.close_reason in {"completed", "failed", "aborted"}
        return cast(CodingHostedOperationState, task.close_reason)
    if assignment.expires_at <= now:
        return "expired"
    if assignment.started_at is not None:
        return "running"
    if assignment.admitted_at is not None:
        return "admitted"
    return "pending_admission"


@router.get("", response_model=AdminCodingControlPlaneResponse)
async def get_coding_control_plane(
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdminCodingControlPlaneResponse:
    """Return bounded native progress and safe feature-gate visibility."""

    response.headers["Cache-Control"] = "no-store"
    total = int(
        await session.scalar(select(func.count()).select_from(CodingHostedAssignment))
        or 0
    )
    rows = (
        await session.execute(
            select(CodingHostedAssignment, CodingHostedPrivateTask)
            .outerjoin(
                CodingHostedPrivateTask,
                CodingHostedPrivateTask.evaluation_id
                == CodingHostedAssignment.evaluation_id,
            )
            .order_by(
                CodingHostedAssignment.created_at.desc(),
                CodingHostedAssignment.evaluation_id.desc(),
            )
            .limit(limit)
        )
    ).all()
    now = datetime.now(UTC)
    operations = [
        CodingHostedOperationRecord(
            evaluation_id=assignment.evaluation_id,
            attempt_id=assignment.attempt_id,
            release_row_id=assignment.release_row_id,
            registration_sha256=assignment.registration_sha256,
            agent_id=assignment.agent_id,
            validator_hotkey=assignment.validator_hotkey,
            artifact_sha256=assignment.artifact_sha256,
            screened_image_sha256=assignment.screened_image_sha256,
            assignment_sha256=assignment.assignment_sha256,
            state=_state(assignment, task, now=now),
            expires_at=assignment.expires_at,
            created_at=assignment.created_at,
            admitted_at=assignment.admitted_at,
            started_at=assignment.started_at,
            frozen=task is not None and task.frozen_at is not None,
            closed_at=task.closed_at if task is not None else None,
            close_reason=task.close_reason if task is not None else None,
            registered_actor=assignment.actor,
            registered_reason=assignment.reason,
            shadow_only=True,
            weight_eligible=False,
        )
        for assignment, task in rows
    ]
    config = request.app.state.config
    return AdminCodingControlPlaneResponse(
        total_native_operations=total,
        native_operations=operations,
        hosted_control_configured=(
            getattr(request.app.state, "coding_hosted_control", None) is not None
        ),
        contract_v1_reconciliation_enabled=(
            config.coding_shadow_reconciliation_enabled
        ),
        contract_v1_ticket_set_enabled=config.coding_shadow_ticket_set_enabled,
        contract_v1_ticket_lease_seconds=(config.coding_shadow_ticket_lease_seconds),
        native_v2_selectable=False,
        shadow_only=True,
        weight_eligible=False,
    )

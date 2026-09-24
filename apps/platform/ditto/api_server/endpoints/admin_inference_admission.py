"""Read sanitized inference admission rejections for one grant."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.inference_admission import (
    InferenceAdmissionRejectionRow,
    InferenceAdmissionRejectionSummary,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.inference_admission import admission_rejection_summary

router = APIRouter(prefix="/admin/inference-admission-rejections", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=InferenceAdmissionRejectionSummary)
async def list_admission_rejections(
    _admin: AdminDep,
    session: SessionDep,
    grant_id: Annotated[UUID, Query()],
) -> InferenceAdmissionRejectionSummary:
    """Counts and recent rows. Bodies, prompts, and credentials are not stored."""
    counts, rows = await admission_rejection_summary(session, grant_id=grant_id)
    return InferenceAdmissionRejectionSummary(
        grant_id=grant_id,
        counts=counts,
        rows=[
            InferenceAdmissionRejectionRow(
                rejection_id=row.rejection_id,
                created_at=row.created_at,
                lane=row.lane,
                http_status=row.http_status,
                admission_code=row.admission_code,
                grant_id=row.grant_id,
                validator_hotkey=row.validator_hotkey,
                correlation_id=row.correlation_id,
                request_bytes=row.request_bytes,
                byte_limit=row.byte_limit,
                platform_revision=row.platform_revision,
            )
            for row in rows
        ],
    )

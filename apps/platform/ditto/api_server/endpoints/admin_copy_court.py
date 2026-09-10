"""Audited operator control and read surface for the copy-hold triage court."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.copy_court_settings import (
    AdminCopyCourtRecommendation,
    AdminCopyCourtRecommendationList,
    AdminCopyCourtSettingsRequest,
    AdminCopyCourtSettingsResponse,
    CopyCourtSettings,
    CopyCourtSettingsRevision,
    copy_court_checksum,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthCopyCourtRecommendation,
    AthReview,
)
from ditto.db.models import (
    CopyCourtSettingsRevision as CopyCourtSettingsRevisionRow,
)

router = APIRouter(prefix="/admin/copy-court", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_MODE = CopyCourtSettings.model_fields["mode"].annotation


def _revision(row: CopyCourtSettingsRevisionRow) -> CopyCourtSettingsRevision:
    return CopyCourtSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=CopyCourtSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


@router.get("/settings", response_model=AdminCopyCourtSettingsResponse)
async def get_copy_court_settings(
    _admin: AdminDep,
    session: SessionDep,
    history_limit: Annotated[int, Query(ge=0, le=200)] = 50,
) -> AdminCopyCourtSettingsResponse:
    """Return the current copy-court posture and its append-only history."""
    rows = list(
        await session.scalars(
            select(CopyCourtSettingsRevisionRow)
            .order_by(CopyCourtSettingsRevisionRow.revision.desc())
            .limit(max(history_limit, 1))
        )
    )
    current = rows[0] if rows else None
    return AdminCopyCourtSettingsResponse(
        current=_revision(current) if current is not None else None,
        history=[_revision(row) for row in rows[:history_limit]],
    )


@router.post("/settings", response_model=CopyCourtSettingsRevision)
async def create_copy_court_settings_revision(
    payload: AdminCopyCourtSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> CopyCourtSettingsRevision:
    """Append one optimistic, idempotency-safe settings revision."""
    apply_confirmation = f"APPLY COPY COURT {payload.settings.mode.upper()}"
    latest = await session.scalar(
        select(CopyCourtSettingsRevisionRow)
        .order_by(CopyCourtSettingsRevisionRow.revision.desc())
        .limit(1)
    )
    actual_revision = latest.revision if latest is not None else 0
    if payload.confirmation != apply_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {apply_confirmation}",
        )
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "copy court settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = CopyCourtSettingsRevisionRow(
        parent_revision=actual_revision,
        settings=payload.settings.model_dump(mode="json"),
        checksum=copy_court_checksum(payload.settings),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "copy court settings changed concurrently; refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)


@router.get("/recommendations", response_model=AdminCopyCourtRecommendationList)
async def list_copy_court_recommendations(
    _admin: AdminDep,
    session: SessionDep,
    pending_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminCopyCourtRecommendationList:
    """Page court recommendations; newest first.

    ``pending_only`` (the default) joins to the live hold state so an
    operator sees only recommendations whose review is still unresolved and
    whose agent is still held. Set it false to page the full feed including
    recommendations for holds an operator has since resolved — the
    shadow-mode calibration record.
    """
    query = select(AthCopyCourtRecommendation).order_by(
        AthCopyCourtRecommendation.created_at.desc(),
        AthCopyCourtRecommendation.recommendation_id.desc(),
    )
    if pending_only:
        query = (
            query.join(
                AthReview, AthReview.review_id == AthCopyCourtRecommendation.review_id
            )
            .join(Agent, Agent.agent_id == AthCopyCourtRecommendation.agent_id)
            .where(
                AthReview.status == "pending",
                Agent.status == AgentStatus.ATH_PENDING_REVIEW,
            )
        )
    rows = list(await session.scalars(query.limit(limit).offset(offset)))
    return AdminCopyCourtRecommendationList(
        items=[_recommendation(row) for row in rows],
        count=len(rows),
        limit=limit,
        offset=offset,
    )


def _recommendation(row: AthCopyCourtRecommendation) -> AdminCopyCourtRecommendation:
    return AdminCopyCourtRecommendation(
        recommendation_id=str(row.recommendation_id),
        review_id=str(row.review_id),
        agent_id=str(row.agent_id),
        verdict=row.verdict,
        hold_class=row.hold_class,
        reason=row.reason,
        citations=row.citations,
        evidence=row.evidence,
        settings_revision=row.settings_revision,
        model=row.model,
        prompt_revision=row.prompt_revision,
        created_at=row.created_at,
    )

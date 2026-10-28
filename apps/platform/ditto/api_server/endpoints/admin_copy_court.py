"""Audited operator control and read surface for the copy-hold triage court."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.copy_court_settings import (
    AdminCopyCourtRecommendation,
    AdminCopyCourtRecommendationList,
    AdminCopyCourtSettingsRequest,
    AdminCopyCourtSettingsResponse,
    AdminCopyCourtTriagePreview,
    CopyCourtSettings,
    CopyCourtSettingsRevision,
    copy_court_checksum,
)
from ditto.api_server.copy_court_triage import TriageUnavailableError
from ditto.api_server.copy_hold_court import CopyHoldCourt
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
    request: Request,
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
    court = getattr(request.app.state, "copy_hold_court", None)
    return AdminCopyCourtSettingsResponse(
        current=_revision(current) if current is not None else None,
        history=[_revision(row) for row in rows[:history_limit]],
        runtime=court.runtime() if isinstance(court, CopyHoldCourt) else None,
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
    agent_id: UUID | None = None,
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
    if agent_id is not None:
        query = query.where(AthCopyCourtRecommendation.agent_id == agent_id)
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
        triage_revision=row.triage_revision,
        application=row.application,
        applied_at=row.applied_at,
        model=row.model,
        prompt_revision=row.prompt_revision,
        created_at=row.created_at,
    )


@router.get(
    "/triage/{agent_id}/preview",
    response_model=AdminCopyCourtTriagePreview,
    responses={
        404: {"description": "No copy-kind ATH review for this agent."},
        409: {"description": "The family triage cannot run in this process."},
        503: {"description": "An artifact could not be read; retry later."},
    },
)
async def preview_copy_court_triage(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminCopyCourtTriagePreview:
    """Dry-run the court's triage for one copy-kind ATH review.

    Works on pending and resolved reviews alike, so operators can calibrate
    the court against settled precedents. Records no recommendation and
    applies nothing; the artifact reads are audited under the court actor.
    The evidence carries paths, line numbers and line digests, never source.
    """
    court = getattr(request.app.state, "copy_hold_court", None)
    if not isinstance(court, CopyHoldCourt):
        raise HTTPException(status_code=409, detail="copy hold court is not running")
    stored_kind = AthReview.algorithm_provenance["review_kind"].as_string()
    review = await session.scalar(
        select(AthReview)
        .where(AthReview.agent_id == agent_id)
        .where(
            stored_kind.is_(None)
            | stored_kind.not_in(
                (
                    "copy",
                    "benchmark_overfit",
                    "deferred_source_review",
                    "anomalous_score",
                )
            )
            | (stored_kind == "copy")
        )
        .order_by(AthReview.opened_at.desc())
        .limit(1)
    )
    if review is None:
        raise HTTPException(status_code=404, detail="no copy-kind ATH review")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        verdict, triage_revision, auto_clear = await court.preview(
            session, review, agent
        )
    except TriageUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return AdminCopyCourtTriagePreview(
        agent_id=str(agent_id),
        review_id=str(review.review_id),
        review_status=review.status,
        hold_class=verdict.hold_class,
        triage_revision=triage_revision,
        verdict=verdict.verdict,
        reason=verdict.reason,
        citations=verdict.citations,
        evidence=verdict.evidence,
        auto_clear_eligible=auto_clear,
    )

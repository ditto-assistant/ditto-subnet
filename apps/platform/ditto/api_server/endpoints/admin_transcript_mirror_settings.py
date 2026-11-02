"""Audited operator control for the public transcript mirror."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.transcript_mirror_settings import (
    AdminTranscriptMirrorSettingsRequest,
    AdminTranscriptMirrorSettingsResponse,
    transcript_mirror_confirmation,
)
from ditto.api_models.transcript_mirror_settings import (
    TranscriptMirrorSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import TranscriptMirrorSettingsRevision
from ditto.db.queries.transcript_mirror_settings import (
    latest_transcript_mirror_settings,
)

router = APIRouter(prefix="/admin/transcript-mirror-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: TranscriptMirrorSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        enabled=row.enabled,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


@router.get("", response_model=AdminTranscriptMirrorSettingsResponse)
async def get_settings(
    _admin: AdminDep,
    session: SessionDep,
) -> AdminTranscriptMirrorSettingsResponse:
    rows = list(
        await session.scalars(
            select(TranscriptMirrorSettingsRevision)
            .order_by(TranscriptMirrorSettingsRevision.revision.desc())
            .limit(100)
        )
    )
    if not rows:
        raise HTTPException(
            status_code=503, detail="transcript mirror settings missing"
        )
    return AdminTranscriptMirrorSettingsResponse(
        current=_revision(rows[0]),
        history=[_revision(row) for row in rows],
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminTranscriptMirrorSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    """Append one audited revision. The mirror stays off until this says otherwise."""
    expected = transcript_mirror_confirmation(payload.enabled)
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected}",
        )
    latest = await latest_transcript_mirror_settings(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "transcript mirror settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = TranscriptMirrorSettingsRevision(
        parent_revision=actual_revision,
        enabled=payload.enabled,
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
                "transcript mirror settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)

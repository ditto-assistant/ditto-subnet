"""Audited operator control for miner submission cooldown."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_settings import (
    AdminSubmissionSettingsRequest,
    AdminSubmissionSettingsResponse,
)
from ditto.api_models.submission_settings import (
    SubmissionSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import SubmissionSettingsRevision
from ditto.db.queries.submission_settings import (
    DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
    DEFAULT_SUBMISSION_FEE_RAO,
    latest_submission_settings,
)

router = APIRouter(prefix="/admin/submission-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: SubmissionSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        cooldown_seconds=row.cooldown_seconds,
        fee_amount_rao=row.fee_amount_rao,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _default_revision() -> RevisionModel:
    return RevisionModel(
        revision=0,
        parent_revision=0,
        cooldown_seconds=DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
        fee_amount_rao=DEFAULT_SUBMISSION_FEE_RAO,
        reason="Built-in submission cooldown and 0.04 TAO fee",
        actor="platform",
        created_at=None,
    )


@router.get("", response_model=AdminSubmissionSettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminSubmissionSettingsResponse:
    rows = list(
        await session.scalars(
            select(SubmissionSettingsRevision)
            .order_by(SubmissionSettingsRevision.revision.desc())
            .limit(100)
        )
    )
    return AdminSubmissionSettingsResponse(
        current=_revision(rows[0]) if rows else _default_revision(),
        history=[_revision(row) for row in rows],
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminSubmissionSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    latest = await latest_submission_settings(session)
    current_fee = (
        latest.fee_amount_rao if latest is not None else DEFAULT_SUBMISSION_FEE_RAO
    )
    fee_amount_rao = payload.fee_amount_rao or current_fee
    expected_confirmation = (
        f"SET SUBMISSION COOLDOWN {payload.cooldown_seconds} SECONDS "
        f"FEE {fee_amount_rao} RAO"
    )
    legacy_confirmation = f"SET SUBMISSION COOLDOWN {payload.cooldown_seconds} SECONDS"
    valid_confirmation = (
        {expected_confirmation, legacy_confirmation}
        if payload.fee_amount_rao is None
        else {expected_confirmation}
    )
    if payload.confirmation not in valid_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "submission settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = SubmissionSettingsRevision(
        parent_revision=actual_revision,
        cooldown_seconds=payload.cooldown_seconds,
        fee_amount_rao=fee_amount_rao,
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
            detail="submission settings changed concurrently; refresh before applying",
        ) from error
    await session.refresh(row)
    return _revision(row)

"""Audited operator control for miner self-serve screening retries (SN-456).

Operators pause the miner route (``enabled``) or change the per-agent budget
here. Every miner-created grant is a normal ``screening_retry_overrides`` row
whose actor starts with ``miner-self-serve:``, so the existing Backroom override
and screening views show them; this read adds the fleet-wide 24h count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_screening_retry import (
    AdminMinerScreeningRetrySettingsRequest,
    AdminMinerScreeningRetrySettingsResponse,
    miner_screening_retry_settings_confirmation,
)
from ditto.api_models.miner_screening_retry import (
    MinerScreeningRetrySettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.miner_screening_retry import (
    MINER_SELF_SERVE_ACTOR_PREFIX,
)
from ditto.db.models import (
    MinerScreeningRetrySettingsRevision,
    ScreeningRetryOverride,
)

router = APIRouter(prefix="/admin/miner-screening-retry-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: MinerScreeningRetrySettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        enabled=row.enabled,
        max_retries_per_agent=row.max_retries_per_agent,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


@router.get("", response_model=AdminMinerScreeningRetrySettingsResponse)
async def get_settings(
    _admin: AdminDep,
    session: SessionDep,
) -> AdminMinerScreeningRetrySettingsResponse:
    rows = list(
        await session.scalars(
            select(MinerScreeningRetrySettingsRevision)
            .order_by(MinerScreeningRetrySettingsRevision.revision.desc())
            .limit(100)
        )
    )
    if not rows:
        raise HTTPException(
            status_code=503, detail="miner screening retry settings missing"
        )
    since = datetime.now(UTC) - timedelta(hours=24)
    recent = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreeningRetryOverride)
            .where(
                ScreeningRetryOverride.actor.startswith(MINER_SELF_SERVE_ACTOR_PREFIX),
                ScreeningRetryOverride.created_at >= since,
            )
        )
        or 0
    )
    return AdminMinerScreeningRetrySettingsResponse(
        current=_revision(rows[0]),
        history=[_revision(row) for row in rows],
        recent_self_serve_retries_24h=recent,
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminMinerScreeningRetrySettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    """Append one audited revision guarded by the revision the operator read."""
    expected = miner_screening_retry_settings_confirmation(
        enabled=payload.enabled, max_retries_per_agent=payload.max_retries_per_agent
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected}",
        )
    latest = await session.scalar(
        select(MinerScreeningRetrySettingsRevision)
        .order_by(MinerScreeningRetrySettingsRevision.revision.desc())
        .limit(1)
    )
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "miner screening retry settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = MinerScreeningRetrySettingsRevision(
        parent_revision=actual_revision,
        enabled=payload.enabled,
        max_retries_per_agent=payload.max_retries_per_agent,
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
                "miner screening retry settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)

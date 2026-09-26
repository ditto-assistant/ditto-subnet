"""Audited shadow-only treasury policy; this endpoint cannot move funds."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_settings import (
    AdminTreasurySettingsRequest,
    TreasurySettings,
    TreasurySettingsControl,
    TreasurySettingsRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import TreasurySettingsRevision as RevisionRow

router = APIRouter(prefix="/admin/treasury-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: RevisionRow) -> TreasurySettingsRevision:
    return TreasurySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=TreasurySettings.model_validate(row.settings),
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


async def _latest(session: AsyncSession) -> RevisionRow | None:
    return await session.scalar(
        select(RevisionRow).order_by(RevisionRow.revision.desc()).limit(1)
    )


@router.get("", response_model=TreasurySettingsControl)
async def get_treasury_settings(
    _admin: AdminDep, session: SessionDep
) -> TreasurySettingsControl:
    latest = await _latest(session)
    history = list(
        await session.scalars(
            select(RevisionRow).order_by(RevisionRow.revision.desc()).limit(200)
        )
    )
    effective = (
        TreasurySettings.model_validate(latest.settings)
        if latest
        else TreasurySettings()
    )
    return TreasurySettingsControl(
        effective=effective,
        revision=latest.revision if latest else 0,
        miner_bps=effective.miner_bps,
        history=[_revision(row) for row in history],
    )


@router.post("", response_model=TreasurySettingsRevision)
async def record_treasury_settings(
    payload: AdminTreasurySettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasurySettingsRevision:
    latest = await _latest(session)
    current = latest.revision if latest else 0
    if payload.expected_revision != current:
        raise HTTPException(status_code=409, detail="treasury policy changed; refresh")
    canonical = json.dumps(
        payload.settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    row = RevisionRow(
        parent_revision=current,
        settings=payload.settings.model_dump(mode="json"),
        checksum=hashlib.sha256(canonical.encode()).hexdigest(),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="treasury policy changed concurrently"
        ) from error
    await session.refresh(row)
    return _revision(row)

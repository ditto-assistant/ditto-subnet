"""Audited hot-swappable control for the subnet-owner emission burn.

Mirrors ``admin_continual_retest_settings``: optimistic concurrency on
``expected_revision``, a typed confirmation phrase, and an append-only revision
that stores the whole policy. The revision log is the point -- this is the one
control here that moves TAO directly, so "who set the burn to 40% and why"
has to be answerable from the database rather than from memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.burn_settings import (
    AdminBurnSettingsRequest,
    AdminBurnSettingsResponse,
    BurnSettings,
    BurnSettingsRevision,
    EffectiveBurnSettings,
)
from ditto.api_server.burn_settings import (
    DEFAULT_SETTINGS,
    BurnSettingsResolver,
    settings_from_row,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import BurnSettingsRevision as RevisionRow
from ditto.db.queries.burn_settings import (
    GLOBAL_SCOPE,
    insert_burn_settings_revision,
    latest_burn_settings_revision,
    list_burn_settings_revisions,
)
from ditto.db.queries.heartbeats import count_live_validators

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/burn-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
CONFIRMATION = "APPLY BURN SETTINGS"


def _checksum(settings: BurnSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> BurnSettingsRevision:
    return BurnSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=BurnSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> BurnSettingsResolver:
    resolver = getattr(request.app.state, "burn_settings", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="burn settings unavailable")
    return resolver


async def _live_validators(session: AsyncSession) -> int | None:
    """Advisory only: the page still has to render when heartbeats are unreadable."""
    try:
        return await count_live_validators(session, now=datetime.now(UTC))
    except SQLAlchemyError:
        logger.warning("could not count live validators", exc_info=True)
        return None


def _effective(
    latest: RevisionRow | None,
    settings: BurnSettings,
    *,
    ttl_seconds: float,
    live_validators: int | None,
) -> EffectiveBurnSettings:
    return EffectiveBurnSettings(
        revision=latest.revision if latest is not None else 0,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=settings,
        checksum=latest.checksum if latest is not None else "",
        source="revision" if latest is not None else "default",
        max_age_seconds=ttl_seconds,
        miner_emission_share=1.0 - settings.burn_share,
        live_validator_count=live_validators,
    )


@router.get("", response_model=AdminBurnSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminBurnSettingsResponse:
    resolver = _resolver(request)
    latest = await latest_burn_settings_revision(session)
    history = await list_burn_settings_revisions(session)
    settings = settings_from_row(latest)
    return AdminBurnSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=_effective(
            latest,
            settings,
            ttl_seconds=resolver.ttl_seconds,
            live_validators=await _live_validators(session),
        ),
    )


@router.post("", response_model=BurnSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminBurnSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> BurnSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    latest = await latest_burn_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "burn settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_burn_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=payload.settings.model_dump(mode="json"),
            checksum=_checksum(payload.settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="burn settings changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    logger.warning(
        "burn share set to %.4f by %s (revision %d): %s",
        payload.settings.burn_share,
        row.actor,
        row.revision,
        row.reason,
    )
    return _revision(row)

"""Audited control for the clean-board partial burn (#2911).

Mirrors ``admin_burn_settings``: optimistic concurrency on
``expected_revision``, a typed confirmation phrase, and an append-only revision
that stores the whole policy, so "who switched it on and why" is answerable
from the database.
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

from ditto.api_models.clean_board import (
    CLEAN_BOARD_PROTOCOL,
    AdminCleanBoardSettingsRequest,
    AdminCleanBoardSettingsResponse,
    CleanBoardSettings,
    CleanBoardSettingsRevision,
    EffectiveCleanBoardSettings,
)
from ditto.api_server.clean_board import (
    DEFAULT_SETTINGS,
    CleanBoardSettingsResolver,
    settings_from_row,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import CleanBoardSettingsRevision as RevisionRow
from ditto.db.queries.clean_board import (
    GLOBAL_SCOPE,
    insert_clean_board_settings_revision,
    latest_clean_board_settings_revision,
    list_clean_board_settings_revisions,
)
from ditto.db.queries.heartbeats import live_weight_setter_fleet_supports_protocol

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/clean-board-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
CONFIRMATION = "APPLY CLEAN BOARD POLICY"


def checksum(settings: CleanBoardSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> CleanBoardSettingsRevision:
    return CleanBoardSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=CleanBoardSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> CleanBoardSettingsResolver:
    resolver = getattr(request.app.state, "clean_board_settings", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="clean board settings unavailable")
    return resolver


async def _served(session: AsyncSession, settings: CleanBoardSettings) -> bool | None:
    """Advisory: whether the ledger serves the fractions under this policy now."""
    if not settings.enabled:
        return False
    try:
        return await live_weight_setter_fleet_supports_protocol(
            session, minimum_protocol=CLEAN_BOARD_PROTOCOL, now=datetime.now(UTC)
        )
    except SQLAlchemyError:
        logger.warning("could not read clean-board fleet readiness", exc_info=True)
        return None


@router.get("", response_model=AdminCleanBoardSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminCleanBoardSettingsResponse:
    resolver = _resolver(request)
    latest = await latest_clean_board_settings_revision(session)
    history = await list_clean_board_settings_revisions(session)
    settings = settings_from_row(latest)
    return AdminCleanBoardSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=EffectiveCleanBoardSettings(
            revision=latest.revision if latest is not None else 0,
            scope=latest.scope if latest is not None else GLOBAL_SCOPE,
            settings=settings,
            checksum=latest.checksum if latest is not None else "",
            source="revision" if latest is not None else "default",
            max_age_seconds=resolver.ttl_seconds,
            served=await _served(session, settings),
        ),
    )


@router.post("", response_model=CleanBoardSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminCleanBoardSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> CleanBoardSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    latest = await latest_clean_board_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "clean board settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    settings = payload.settings
    try:
        row = await insert_clean_board_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=settings.model_dump(mode="json"),
            checksum=checksum(settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="clean board settings changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    logger.warning(
        "clean board policy set by %s (revision %d): enabled=%s share=%.4f "
        "grace=%ds: %s",
        row.actor,
        row.revision,
        settings.enabled,
        settings.unattested_slot_burn_share,
        settings.attestation_grace_seconds,
        row.reason,
    )
    return _revision(row)

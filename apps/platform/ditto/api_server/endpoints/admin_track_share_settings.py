"""Audited control for the per-track emission split served to validators.

Mirrors ``admin_burn_settings``: optimistic concurrency on
``expected_revision``, an exact confirmation phrase, and an append-only
revision that stores the whole policy. The phrase names the split it applies
(``APPLY TRACK SHARES memory=9000 generator=1000``), so an operator cannot apply
a split they did not type.
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

from ditto.api_models.track_share_settings import (
    BASIS_POINT_SCALE,
    TRACK_SHARES_PROTOCOL,
    AdminTrackShareSettingsRequest,
    AdminTrackShareSettingsResponse,
    EffectiveTrackShareSettings,
    TrackShareSettings,
    TrackShareSettingsRevision,
    track_shares_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.track_share_settings import (
    DEFAULT_SETTINGS,
    TrackShareSettingsResolver,
    settings_from_row,
)
from ditto.api_server.treasury_runtime import lock_runtime
from ditto.db.models import TrackShareSettingsRevision as RevisionRow
from ditto.db.queries.emission_allocation import latest_emission_allocation_number
from ditto.db.queries.heartbeats import (
    count_live_validators,
    live_weight_setter_fleet_supports_protocol,
)
from ditto.db.queries.track_share_settings import (
    GLOBAL_SCOPE,
    insert_track_share_settings_revision,
    latest_track_share_settings_revision,
    list_track_share_settings_revisions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/track-share-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
COMPILED_SPLIT_BPS = {"memory": BASIS_POINT_SCALE}
"""What every validator folds while nothing is served."""


def _checksum(settings: TrackShareSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> TrackShareSettingsRevision:
    return TrackShareSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=settings_from_row(row),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> TrackShareSettingsResolver:
    resolver = getattr(request.app.state, "track_share_settings", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="track share settings unavailable")
    return resolver


async def _served(session: AsyncSession, settings: TrackShareSettings) -> bool | None:
    """Advisory: whether the configured map passes the protocol-33 fleet gate."""
    if settings.track_shares_bps is None:
        return False
    try:
        return await live_weight_setter_fleet_supports_protocol(
            session, minimum_protocol=TRACK_SHARES_PROTOCOL, now=datetime.now(UTC)
        )
    except SQLAlchemyError:
        logger.warning("could not read track-share fleet readiness", exc_info=True)
        return None


async def _live_validators(session: AsyncSession) -> int | None:
    try:
        return await count_live_validators(session, now=datetime.now(UTC))
    except SQLAlchemyError:
        logger.warning("could not count live validators", exc_info=True)
        return None


async def _generator_mode(session: AsyncSession) -> str | None:
    try:
        return (await load_settings(session)).settings.mode
    except SQLAlchemyError:
        logger.warning("could not read generator settings mode", exc_info=True)
        return None


@router.get("", response_model=AdminTrackShareSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminTrackShareSettingsResponse:
    resolver = _resolver(request)
    latest = await latest_track_share_settings_revision(session)
    history = await list_track_share_settings_revisions(session)
    settings = settings_from_row(latest)
    served = await _served(session, settings)
    # Unknown readiness (``served is None``) leaves the in-force split
    # unknown rather than asserting either one.
    in_force: dict[str, int] | None
    if served is None:
        in_force = None
    elif served and settings.track_shares_bps is not None:
        in_force = dict(settings.track_shares_bps)
    else:
        in_force = dict(COMPILED_SPLIT_BPS)
    return AdminTrackShareSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=EffectiveTrackShareSettings(
            revision=latest.revision if latest is not None else 0,
            scope=latest.scope if latest is not None else GLOBAL_SCOPE,
            settings=settings,
            checksum=latest.checksum if latest is not None else "",
            source="revision" if latest is not None else "default",
            max_age_seconds=resolver.ttl_seconds,
            in_force_bps=in_force,
            unallocated_bps=(
                BASIS_POINT_SCALE - sum(in_force.values())
                if in_force is not None
                else None
            ),
            served=served,
            live_validator_count=await _live_validators(session),
            generator_mode=await _generator_mode(session),
            confirmation_hint=track_shares_confirmation(settings.track_shares_bps),
        ),
    )


@router.post("", response_model=TrackShareSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminTrackShareSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> TrackShareSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < 8:
        raise HTTPException(
            status_code=422, detail="reason must be at least 8 characters"
        )
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="actor must be 1-120 characters")
    expected = track_shares_confirmation(payload.settings.track_shares_bps)
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    # Serialize with the emission-allocation writer, which takes the same lock,
    # so an allocation cannot commit between this check and the insert.
    await lock_runtime(session)
    owner = await latest_emission_allocation_number(session)
    if owner:
        raise HTTPException(
            status_code=409,
            detail=(
                f"emission allocation revision {owner} owns the track split; "
                "change it with set_emission_allocation"
            ),
        )
    latest = await latest_track_share_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "track share settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    settings = payload.settings
    try:
        row = await insert_track_share_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=settings.model_dump(mode="json"),
            checksum=_checksum(settings),
            reason=reason,
            actor=actor,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="track share settings changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    logger.warning(
        "track shares set to %s by %s (revision %d): %s",
        settings.track_shares_bps,
        row.actor,
        row.revision,
        row.reason,
    )
    return _revision(row)

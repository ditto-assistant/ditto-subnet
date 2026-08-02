"""Audited, hot-swappable operator control over concurrent benchmark slots.

Append-only revisions of the full slot policy (the per-validator cap plus the
disk circuit breaker). Ticket dispatch reads the latest revision at run time
(short TTL, see ``ditto.api_server.validator_slot_settings``), so an operator can
cap or uncap fleet parallelism live from backroom with no redeploy -- the kill
switch when multi-slot dispatch misbehaves, and the ramp control when it does
not. Modeled on ``admin_efficiency_bonus_settings`` /
``admin_continual_retest_settings``: optimistic concurrency + a typed
confirmation string + an actor/reason audit trail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.validator_slot_settings import (
    AdminValidatorSlotSettingsRequest,
    AdminValidatorSlotSettingsResponse,
    ValidatorSlotSettings,
    ValidatorSlotSettingsRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS,
    ValidatorSlotSettingsResolver,
    effective_view,
)
from ditto.db.models import ValidatorSlotSettingsRevision as RevisionRow
from ditto.db.queries.validator_slot_settings import (
    GLOBAL_SCOPE,
    insert_validator_slot_settings_revision,
    latest_validator_slot_settings_revision,
    list_validator_slot_settings_revisions,
)

router = APIRouter(prefix="/admin/validator-slot-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def confirmation_for(settings: ValidatorSlotSettings) -> str:
    """The exact string an operator must type to apply ``settings``.

    It names the resulting cap, so the number is stated twice in one request and
    a mistyped ramp cannot land silently.
    """
    return f"APPLY VALIDATOR SLOT CAP {settings.max_concurrent_slots}"


def _checksum(settings: ValidatorSlotSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> ValidatorSlotSettingsRevision:
    return ValidatorSlotSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=ValidatorSlotSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> ValidatorSlotSettingsResolver:
    resolver = getattr(request.app.state, "validator_slot_settings", None)
    if resolver is None:  # pragma: no cover - always wired in create_api_server
        raise HTTPException(
            status_code=503, detail="validator slot settings are not configured"
        )
    return resolver


@router.get("", response_model=AdminValidatorSlotSettingsResponse)
async def get_settings(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminValidatorSlotSettingsResponse:
    """Current policy, append-only history, the module default, and the settings
    actually in force right now (built from a fresh read, not the TTL cache)."""
    resolver = _resolver(request)
    latest = await latest_validator_slot_settings_revision(session)
    history = await list_validator_slot_settings_revisions(session)
    return AdminValidatorSlotSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=effective_view(latest, ttl_seconds=resolver.ttl_seconds),
    )


@router.post("", response_model=ValidatorSlotSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminValidatorSlotSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ValidatorSlotSettingsRevision:
    """Append one optimistic, confirmation-gated revision, then invalidate the
    dispatch-path cache so the change lands on the next ticket issue."""
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(
            status_code=422,
            detail="validator slot policy is subnet-global; scope must be '*'",
        )
    expected_confirmation = confirmation_for(payload.settings)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_validator_slot_settings_revision(session, scope=payload.scope)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "validator slot settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_validator_slot_settings_revision(
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
            detail=(
                "validator slot settings changed concurrently; refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    # Land the change immediately on this worker; other workers converge within
    # the resolver TTL.
    _resolver(request).invalidate()
    return _revision(row)

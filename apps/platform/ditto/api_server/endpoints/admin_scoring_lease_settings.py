"""Audited operator control for scoring lease clocks (#1156).

Append-only revisions of the canonical scoring ticket TTL, which also governs
score-retest replacement tickets. It used to be a code constant, so every retune
was a deploy.

The revision applies to NEW leases only: the TTL is stamped onto a ticket's
``deadline`` when it is minted and a live ticket is resumed untouched, so a
lower TTL never shortens work already running and a higher one never extends it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.scoring_lease_settings import (
    AdminScoringLeaseSettingsRequest,
    AdminScoringLeaseSettingsResponse,
    EffectiveScoringLeaseSettings,
    ScoringLeaseSettings,
    ScoringLeaseSettingsRevision,
    scoring_lease_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.scoring_lease_settings import (
    DEFAULT_SETTINGS,
    DEFAULT_SETTINGS_TTL_SECONDS,
    ScoringLeaseSettingsResolver,
    settings_from_row,
)
from ditto.db.models import ScoringLeaseSettingsRevision as RevisionRow
from ditto.db.queries.scoring_lease_settings import (
    GLOBAL_SCOPE,
    insert_scoring_lease_settings_revision,
    latest_scoring_lease_settings_revision,
    list_scoring_lease_settings_revisions,
)

router = APIRouter(prefix="/admin/scoring-lease-settings", tags=["admin"])
MIN_REASON_LENGTH = 8
MAX_ACTOR_LENGTH = 120
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _checksum(settings: ScoringLeaseSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> ScoringLeaseSettingsRevision:
    try:
        settings = ScoringLeaseSettings.model_validate(row.settings)
        settings_valid = True
    except ValidationError:
        settings = settings_from_row(row)
        settings_valid = False
    return ScoringLeaseSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=settings,
        settings_valid=settings_valid,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> ScoringLeaseSettingsResolver | None:
    return getattr(request.app.state, "scoring_lease_settings", None)


def _effective(
    latest: RevisionRow | None, *, request: Request
) -> EffectiveScoringLeaseSettings:
    resolver = _resolver(request)
    revision = _revision(latest) if latest is not None else None
    return EffectiveScoringLeaseSettings(
        revision=latest.revision if latest is not None else 0,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=revision.settings if revision is not None else DEFAULT_SETTINGS,
        checksum=latest.checksum if latest is not None else "",
        source="revision"
        if revision is not None and revision.settings_valid
        else "default",
        settings_valid=revision.settings_valid if revision is not None else True,
        max_age_seconds=(
            resolver.ttl_seconds
            if resolver is not None
            else DEFAULT_SETTINGS_TTL_SECONDS
        ),
    )


@router.get("", response_model=AdminScoringLeaseSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminScoringLeaseSettingsResponse:
    """Current policy, append-only history, the default, and what is in force."""
    latest = await latest_scoring_lease_settings_revision(session)
    history = await list_scoring_lease_settings_revisions(session)
    return AdminScoringLeaseSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=_effective(latest, request=request),
    )


@router.post("", response_model=ScoringLeaseSettingsRevision)
async def create_settings_revision(
    payload: AdminScoringLeaseSettingsRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> ScoringLeaseSettingsRevision:
    """Append one optimistic, confirmation-gated revision.

    Reaches ticket issuance within the resolver TTL, fleet-wide, with no
    restart. Live tickets keep the deadline they were minted with.
    """
    # Audit fields are validated on their TRIMMED values, first, so blank or
    # whitespace-padded input is a clear 422 and never reaches the revision
    # check or the table's trimmed-length constraints (which would surface as
    # a misleading "changed concurrently" 409 that no refresh can fix).
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    expected_confirmation = scoring_lease_confirmation(
        payload.settings.scoring_ticket_ttl_minutes
    )
    if len(reason) < MIN_REASON_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"reason must be at least {MIN_REASON_LENGTH} characters after "
                "trimming whitespace"
            ),
        )
    if not 1 <= len(actor) <= MAX_ACTOR_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"actor must be 1-{MAX_ACTOR_LENGTH} characters after trimming "
                "whitespace"
            ),
        )
    if not payload.confirmation.strip():
        raise HTTPException(
            status_code=422,
            detail=f"confirmation is required: type exactly {expected_confirmation}",
        )
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(
            status_code=422,
            detail="scoring lease settings are subnet-global; scope must be '*'",
        )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_scoring_lease_settings_revision(session, scope=payload.scope)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "scoring lease settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_scoring_lease_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=payload.settings.model_dump(mode="json"),
            checksum=_checksum(payload.settings),
            reason=reason,
            actor=actor,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="scoring lease settings changed concurrently; refresh and retry",
        ) from error
    resolver = _resolver(request)
    if resolver is not None:
        resolver.invalidate()
    await session.refresh(row)
    return _revision(row)

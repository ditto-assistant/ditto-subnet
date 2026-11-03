"""Audited operator control for miner submission cooldown and fees.

One revision holds the coldkey cooldown, the default fee (the memory lane's
price) and the per-competition fee overrides; see
:mod:`ditto.api_models.submission_settings`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.owner_submission_cooldown import (
    AdminOwnerCooldownsResponse,
    AdminRevokeOwnerCooldownRequest,
    AdminSetOwnerCooldownRequest,
    OwnerCooldownLookup,
    OwnerCooldownRevision,
    OwnerScope,
    revoke_owner_cooldown_confirmation,
    set_owner_cooldown_confirmation,
)
from ditto.api_models.submission_settings import (
    UPLOAD_COMPETITIONS,
    AdminSubmissionSettingsRequest,
    AdminSubmissionSettingsResponse,
    CompetitionFeePolicy,
    EffectiveCompetitionFee,
    competition_fees_confirmation,
)
from ditto.api_models.submission_settings import (
    SubmissionSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import load_settings
from ditto.db.models import (
    OwnerSubmissionCooldownRevision,
    SubmissionSettingsRevision,
)
from ditto.db.queries.owner_submission_cooldown import (
    count_active_overrides,
    current_override,
    identity_latest_submission_at,
    latest_overrides,
    override_history,
    override_state,
)
from ditto.db.queries.submission_settings import (
    DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
    DEFAULT_SUBMISSION_FEE_RAO,
    CompetitionFeeOverride,
    dump_competition_fees,
    latest_submission_settings,
    parse_competition_fees,
    resolve_submission_settings,
)

router = APIRouter(prefix="/admin/submission-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _policies(
    overrides: dict[str, CompetitionFeeOverride],
) -> dict[str, CompetitionFeePolicy]:
    return {
        competition: CompetitionFeePolicy(
            fee_amount_rao=override.fee_amount_rao,
            fee_required=override.fee_required,
        )
        for competition, override in overrides.items()
    }


def _revision(row: SubmissionSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        cooldown_seconds=row.cooldown_seconds,
        fee_amount_rao=row.fee_amount_rao,
        competition_fees=_policies(parse_competition_fees(row.competition_fees)),
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
        reason="Built-in submission cooldown and 0.2 TAO fee",
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
    latest = rows[0] if rows else None
    # The address is not part of this view; the deposit-address control owns
    # it.
    resolved = resolve_submission_settings(latest, payment_address="")
    generator = (await load_settings(session)).settings
    lane_switches = {"generator": generator.upload_fee_required}
    effective = []
    for competition in UPLOAD_COMPETITIONS:
        fee = resolved.competition_fee(
            competition,
            competition_requires_fee=lane_switches.get(competition, True),
        )
        effective.append(
            EffectiveCompetitionFee(
                competition=fee.competition,
                fee_amount_rao=fee.fee_amount_rao,
                amount_source=fee.amount_source,
                fee_required=fee.fee_required,
                required_source=fee.required_source,
            )
        )
    return AdminSubmissionSettingsResponse(
        current=_revision(latest) if latest is not None else _default_revision(),
        history=[_revision(row) for row in rows],
        effective_competition_fees=effective,
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
    # Omitted overrides carry forward unchanged and leave the phrase exactly
    # as it was before per-competition fees, so a client that never sends
    # them keeps working. An explicit map is the whole map, and is echoed.
    if payload.competition_fees is None:
        competition_fees = dump_competition_fees(
            parse_competition_fees(latest.competition_fees)
            if latest is not None
            else {}
        )
    else:
        competition_fees = {
            competition: policy.model_dump()
            for competition, policy in payload.competition_fees.items()
        }
    expected_confirmation = (
        f"SET SUBMISSION COOLDOWN {payload.cooldown_seconds} SECONDS "
        f"FEE {fee_amount_rao} RAO"
    )
    if payload.competition_fees is not None:
        expected_confirmation += competition_fees_confirmation(payload.competition_fees)
    legacy_confirmation = f"SET SUBMISSION COOLDOWN {payload.cooldown_seconds} SECONDS"
    valid_confirmation = (
        {expected_confirmation, legacy_confirmation}
        if payload.fee_amount_rao is None and payload.competition_fees is None
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
        competition_fees=competition_fees,
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


# --- Operator per-owner cooldown overrides -------------------------------


def _owner_revision(
    row: OwnerSubmissionCooldownRevision, *, now: datetime
) -> OwnerCooldownRevision:
    return OwnerCooldownRevision(
        scope=row.scope,  # type: ignore[arg-type]
        owner_key=row.owner_key,
        revision=row.revision,
        action=row.action,  # type: ignore[arg-type]
        cooldown_seconds=row.cooldown_seconds,
        expires_at=row.expires_at,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        state=override_state(row, now=now),
    )


async def _owner_lookup(
    session: AsyncSession, *, scope: OwnerScope, owner_key: str, now: datetime
) -> OwnerCooldownLookup:
    history = await override_history(session, scope=scope, owner_key=owner_key)
    current = history[0] if history else None
    latest_submission = await identity_latest_submission_at(
        session, scope=scope, owner_key=owner_key, now=now
    )
    next_allowed_at = None
    if (
        current is not None
        and latest_submission is not None
        and override_state(current, now=now) == "active"
    ):
        assert current.cooldown_seconds is not None
        assert current.expires_at is not None
        candidate = min(
            latest_submission + timedelta(seconds=current.cooldown_seconds),
            current.expires_at,
        )
        next_allowed_at = candidate if candidate > now else None
    resolved = resolve_submission_settings(
        await latest_submission_settings(session), payment_address=""
    )
    return OwnerCooldownLookup(
        scope=scope,
        owner_key=owner_key,
        current=_owner_revision(current, now=now) if current is not None else None,
        global_cooldown_seconds=resolved.cooldown_seconds,
        latest_submission_at=latest_submission,
        next_allowed_at=next_allowed_at,
        history=[_owner_revision(row, now=now) for row in history],
    )


@router.get("/owner-cooldowns", response_model=AdminOwnerCooldownsResponse)
async def get_owner_cooldowns(
    _admin: AdminDep,
    session: SessionDep,
    scope: OwnerScope | None = None,
    owner_key: Annotated[str | None, Query(max_length=128)] = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> AdminOwnerCooldownsResponse:
    """List operator per-owner submission cooldown overrides.

    Read-only. With ``scope`` and ``owner_key`` the response also carries that
    identity's full revision history, its newest submission and when the
    override next lets it submit.
    """
    if (scope is None) != (owner_key is None):
        raise HTTPException(
            status_code=422, detail="scope and owner_key must be given together"
        )
    now = datetime.now(UTC)
    fetched = await latest_overrides(
        session, now=now, active_only=not include_inactive, limit=limit + 1
    )
    rows = [_owner_revision(row, now=now) for row in fetched[:limit]]
    lookup = None
    if scope is not None and owner_key is not None:
        lookup = await _owner_lookup(
            session, scope=scope, owner_key=owner_key.strip(), now=now
        )
    return AdminOwnerCooldownsResponse(
        active_count=await count_active_overrides(session, now=now),
        items=rows,
        truncated=len(fetched) > limit,
        lookup=lookup,
    )


async def _append_owner_revision(
    session: AsyncSession,
    *,
    scope: str,
    owner_key: str,
    expected_revision: int,
    row: OwnerSubmissionCooldownRevision,
) -> None:
    current = await current_override(
        session,
        scope=scope,  # type: ignore[arg-type]
        owner_key=owner_key,
    )
    actual = current.revision if current is not None else 0
    if expected_revision != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                "owner submission cooldown changed; refresh before applying "
                f"(expected revision {expected_revision}, current {actual})"
            ),
        )
    row.revision = actual + 1
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "owner submission cooldown changed concurrently; refresh before "
                "applying"
            ),
        ) from error
    await session.refresh(row)


@router.post("/owner-cooldowns", response_model=OwnerCooldownLookup)
async def set_owner_cooldown(
    payload: AdminSetOwnerCooldownRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> OwnerCooldownLookup:
    """Install or replace one owner identity's cooldown override (CAS)."""
    expected = set_owner_cooldown_confirmation(
        scope=payload.scope,
        owner_key=payload.owner_key,
        cooldown_seconds=payload.cooldown_seconds,
        expires_in_days=payload.expires_in_days,
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    now = datetime.now(UTC)
    row = OwnerSubmissionCooldownRevision(
        scope=payload.scope,
        owner_key=payload.owner_key,
        action="set",
        cooldown_seconds=payload.cooldown_seconds,
        expires_at=now + timedelta(days=payload.expires_in_days),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
        created_at=now,
    )
    await _append_owner_revision(
        session,
        scope=payload.scope,
        owner_key=payload.owner_key,
        expected_revision=payload.expected_revision,
        row=row,
    )
    return await _owner_lookup(
        session, scope=payload.scope, owner_key=payload.owner_key, now=now
    )


@router.post("/owner-cooldowns/revoke", response_model=OwnerCooldownLookup)
async def revoke_owner_cooldown(
    payload: AdminRevokeOwnerCooldownRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> OwnerCooldownLookup:
    """End one owner identity's active override early (CAS)."""
    expected = revoke_owner_cooldown_confirmation(
        scope=payload.scope, owner_key=payload.owner_key
    )
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    now = datetime.now(UTC)
    current = await current_override(
        session, scope=payload.scope, owner_key=payload.owner_key
    )
    if current is None or override_state(current, now=now) != "active":
        raise HTTPException(
            status_code=409,
            detail="no active owner submission cooldown to revoke",
        )
    row = OwnerSubmissionCooldownRevision(
        scope=payload.scope,
        owner_key=payload.owner_key,
        action="revoke",
        cooldown_seconds=None,
        expires_at=None,
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
        created_at=now,
    )
    await _append_owner_revision(
        session,
        scope=payload.scope,
        owner_key=payload.owner_key,
        expected_revision=payload.expected_revision,
        row=row,
    )
    return await _owner_lookup(
        session, scope=payload.scope, owner_key=payload.owner_key, now=now
    )

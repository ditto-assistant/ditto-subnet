"""Audited, hot-swappable operator control for the relative token-efficiency
bonus (bench_version >= 7).

Append-only revisions of the full bonus policy (both booleans + all eight
numeric knobs) that #403 read from ``DITTO_EFFICIENCY_BONUS_*`` at boot. The
compute path reads the latest revision at run time (short TTL, see
``ditto.api_server.efficiency_settings``), so an operator can enable / disable /
fold the bonus and retune every knob live from backroom with no redeploy — while
every published epoch snapshot keeps its own frozen knobs, so a later change
never mutates an already-frozen bonus. Modeled on
``admin_screener_review_settings`` / ``admin_benchmark_rollout``: optimistic
concurrency + a typed confirmation string + an actor/reason audit trail.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.efficiency_settings import (
    AdminEfficiencyBonusSettingsRequest,
    AdminEfficiencyBonusSettingsResponse,
    EfficiencyBonusSettings,
    EfficiencyBonusSettingsRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.efficiency_settings import (
    EfficiencyBonusSettingsResolver,
    effective_view,
    seed_settings,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import EfficiencyBonusSettingsRevision as RevisionRow
from ditto.db.queries.efficiency_settings import (
    GLOBAL_SCOPE,
    insert_efficiency_settings_revision,
    latest_efficiency_settings_revision,
    list_efficiency_settings_revisions,
)

router = APIRouter(prefix="/admin/efficiency-bonus-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _checksum(settings: EfficiencyBonusSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> EfficiencyBonusSettingsRevision:
    return EfficiencyBonusSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=EfficiencyBonusSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> EfficiencyBonusSettingsResolver:
    resolver = getattr(request.app.state, "efficiency_settings", None)
    if resolver is None:  # pragma: no cover - always wired in create_api_server
        raise HTTPException(
            status_code=503, detail="efficiency bonus settings are not configured"
        )
    return resolver


@router.get("", response_model=AdminEfficiencyBonusSettingsResponse)
async def get_settings(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminEfficiencyBonusSettingsResponse:
    """Current policy, append-only history, the env seed, and the settings
    actually in force right now (built from a fresh read, not the TTL cache)."""
    resolver = _resolver(request)
    latest = await latest_efficiency_settings_revision(session)
    history = await list_efficiency_settings_revisions(session)
    return AdminEfficiencyBonusSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        seed_default=seed_settings(resolver.seed),
        effective=effective_view(
            resolver.seed, latest, ttl_seconds=resolver.ttl_seconds
        ),
    )


@router.post("", response_model=EfficiencyBonusSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminEfficiencyBonusSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> EfficiencyBonusSettingsRevision:
    """Append one optimistic, confirmation-gated revision, then invalidate the
    compute-path cache so the change lands on the next read."""
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(
            status_code=422,
            detail="efficiency bonus policy is subnet-global; scope must be '*'",
        )
    expected_confirmation = (
        "APPLY EFFICIENCY BONUS "
        f"{'ENABLED' if payload.settings.enabled else 'DISABLED'}"
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_efficiency_settings_revision(session, scope=payload.scope)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "efficiency bonus settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_efficiency_settings_revision(
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
                "efficiency bonus settings changed concurrently; refresh before "
                "applying"
            ),
        ) from error
    await session.refresh(row)
    # Land the change immediately on this worker; other workers converge within
    # the resolver TTL.
    _resolver(request).invalidate()
    return _revision(row)

"""Audited hot-swappable controls for continual top-five retesting."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.continual_retest_settings import (
    AdminContinualRetestSettingsRequest,
    AdminContinualRetestSettingsResponse,
    ContinualRetestSettings,
    ContinualRetestSettingsRevision,
    EffectiveContinualRetestSettings,
)
from ditto.api_server.continual_retest_settings import (
    DEFAULT_SETTINGS,
    ContinualRetestSettingsResolver,
    aggregate_is_active,
    rollout_standdown_reason,
    settings_from_row,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import ContinualRetestSettingsRevision as RevisionRow
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto.db.queries.continual_retest_settings import (
    GLOBAL_SCOPE,
    insert_continual_retest_settings_revision,
    latest_continual_retest_settings_revision,
    list_continual_retest_settings_revisions,
)
from ditto.db.queries.heartbeats import live_validator_fleet_supports_protocol
from ditto.db.queries.scores import list_eligible_ledger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/continual-retest-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_REQUIRED_PROTOCOL = 14
_FRESHNESS = timedelta(minutes=15)


def _checksum(settings: ContinualRetestSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> ContinualRetestSettingsRevision:
    return ContinualRetestSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=ContinualRetestSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> ContinualRetestSettingsResolver:
    resolver = getattr(request.app.state, "continual_retest_settings", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail="continual retest settings unavailable",
        )
    return resolver


async def _fleet_ready(session: AsyncSession) -> bool:
    bench_version = await active_bench_version(session)
    return await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=_REQUIRED_PROTOCOL,
        bench_version=bench_version,
        now=datetime.now(UTC),
        freshness=_FRESHNESS,
    )


async def _eligible_agent_count(session: AsyncSession) -> int:
    """Ranked agents the cohort could draw from on the active generation.

    The operator can ask for 25 in a field of nine; without this the page would
    show a cohort size that silently means "everyone" and give no way to tell.
    """
    rows = await list_eligible_ledger(
        session,
        include_fingerprints=False,
        include_details=False,
        bench_version=await active_bench_version(session),
    )
    return sum(1 for row in rows if row.eligible and row.composite > 0.0)


async def _resolved_cohort_size(
    session: AsyncSession, settings: ContinualRetestSettings
) -> int | None:
    """How many agents the cohort rule admits on the board as it stands now.

    ``retest_cohort_size`` is the operator's *request*; under
    ``retest_eligibility_mode="statistical"`` the tie band can admit more. An
    operator tuning ``retest_eligibility_z`` needs to see what the number they
    typed actually produced, otherwise the dial is calibrated by reading
    validator 409s.

    Imported inside the function: ``endpoints.validator`` imports this package's
    resolver, so a module-level import would close a cycle. This mirrors how
    ``_current_koth_entries`` already reaches into ``endpoints.scoring``.
    """
    from ditto.api_server.endpoints.validator import _current_retest_cohort

    try:
        _emission, _wave_members, cohort = await _current_retest_cohort(
            session,
            canonical_version=await active_bench_version(session),
            settings=settings,
        )
    except SQLAlchemyError:
        logger.warning("could not resolve the retest cohort size", exc_info=True)
        return None
    return len(cohort)


@router.get("", response_model=AdminContinualRetestSettingsResponse)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminContinualRetestSettingsResponse:
    resolver = _resolver(request)
    latest = await latest_continual_retest_settings_revision(session)
    history = await list_continual_retest_settings_revisions(session)
    settings = settings_from_row(latest)
    fleet_ready = await _fleet_ready(session)
    rollout = await open_rollout(session)
    desired_version = rollout.desired_version if rollout is not None else None
    # Reported for the fleet, so the operator sees the stand-down that a
    # rollout-capable validator hits rather than guessing why retests went quiet.
    standdown_active = (
        rollout_standdown_reason(
            settings,
            open_rollout_desired_version=desired_version,
            validator_supports_desired_version=True,
        )
        is not None
    )
    return AdminContinualRetestSettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=EffectiveContinualRetestSettings(
            revision=latest.revision if latest is not None else 0,
            scope=latest.scope if latest is not None else GLOBAL_SCOPE,
            settings=settings,
            checksum=latest.checksum if latest is not None else "",
            source="revision" if latest is not None else "default",
            fleet_protocol_ready=fleet_ready,
            aggregate_active=aggregate_is_active(
                settings, fleet_protocol_ready=fleet_ready
            ),
            max_age_seconds=resolver.ttl_seconds,
            open_rollout_desired_version=desired_version,
            rollout_standdown_active=standdown_active,
            resolved_cohort_size=await _resolved_cohort_size(session, settings),
            eligible_agent_count=await _eligible_agent_count(session),
        ),
    )


@router.post("", response_model=ContinualRetestSettingsRevision)
async def create_settings_revision(
    request: Request,
    payload: AdminContinualRetestSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ContinualRetestSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    expected_confirmation = "APPLY CONTINUAL RETEST SETTINGS"
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_continual_retest_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "continual retest settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_continual_retest_settings_revision(
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
            detail="continual retest settings changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    return _revision(row)

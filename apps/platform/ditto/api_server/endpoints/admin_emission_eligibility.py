"""Audited operator control for the terminal-review emission gate.

Mirrors ``admin_burn_settings`` exactly -- optimistic concurrency on
``expected_revision``, a typed confirmation phrase, an append-only revision
storing the whole posture, an actor and a reason -- because it has the same blast
radius: it decides which scored artifacts the validator fold may pay.

Reads:

* ``GET /admin/emission-eligibility`` -- the posture, its history, the fallback
  default, the current and next window boundaries, and the newest shadow
  rehearsal rows. One call answers "what is the posture, what would enforcing it
  cost, and when would a clear I record now take effect".
* ``GET /admin/agents/{agent_id}/emission-eligibility`` -- one exact artifact's
  eligibility record, whether it is in the pool the fold reads, and its own
  rehearsal history. This is the read a miner appeal is answered from.

Neither read grants anything and neither mutates state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.emission_eligibility import (
    DEFAULT_SETTINGS,
    AdminAgentEmissionEligibilityResponse,
    AdminEmissionEligibilitySettingsRequest,
    AdminEmissionEligibilitySettingsResponse,
    AdminEmissionEligibilityShadowRecord,
    EffectiveEmissionEligibilitySettings,
    EmissionEligibilitySettings,
    EmissionEligibilitySettingsRevision,
    eligibility_checksum,
    next_window_start,
    window_start,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import (
    EmissionEligibilityResolver,
    classify,
    policy_from_row,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent
from ditto.db.models import (
    EmissionEligibilitySettingsRevision as RevisionRow,
)
from ditto.db.models import (
    EmissionEligibilityShadowRecord as ShadowRow,
)
from ditto.db.queries.emission_eligibility import (
    GLOBAL_SCOPE,
    AgentReviewPosture,
    count_shadow_records_in_window,
    insert_eligibility_settings_revision,
    latest_eligibility_settings_revision,
    list_eligibility_settings_revisions,
    list_shadow_records,
    load_review_postures,
)
from ditto.db.queries.heartbeats import count_live_validators
from ditto.db.queries.scores import list_eligible_ledger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

CONFIRMATION = "APPLY EMISSION ELIGIBILITY"
"""Typed verbatim on the write. The phrase is deliberately not mode-specific:
every field here can change who is paid, not just ``enforcement``."""

_SHADOW_PREVIEW_LIMIT = 50


def _revision(row: RevisionRow) -> EmissionEligibilitySettingsRevision:
    return EmissionEligibilitySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=EmissionEligibilitySettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _shadow_record(row: ShadowRow) -> AdminEmissionEligibilityShadowRecord:
    return AdminEmissionEligibilityShadowRecord(
        agent_id=row.agent_id,
        artifact_sha256=row.artifact_sha256,
        bench_version=row.bench_version or None,
        state=row.state,  # type: ignore[arg-type]
        reason=row.reason,
        policy_revision=row.policy_revision,
        policy_checksum=row.policy_checksum,
        enforcement=row.enforcement,  # type: ignore[arg-type]
        window_start=row.window_start,
        created_at=row.created_at,
    )


def _resolver(request: Request) -> EmissionEligibilityResolver:
    resolver = getattr(request.app.state, "emission_eligibility", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(
            status_code=503, detail="emission eligibility settings unavailable"
        )
    return resolver


async def _advisory_count(coroutine_factory: Callable) -> int | None:
    """Advisory only: the posture page still has to render when a side read
    fails. A failure here must never hide the posture itself."""
    try:
        return await coroutine_factory()
    except SQLAlchemyError:
        logger.warning("emission eligibility advisory read failed", exc_info=True)
        return None


async def _effective(
    session: AsyncSession,
    latest: RevisionRow | None,
    *,
    ttl_seconds: float,
    now: datetime,
) -> EffectiveEmissionEligibilitySettings:
    policy = policy_from_row(latest)
    settings = policy.settings
    window = window_start(now, window_seconds=settings.activation_window_seconds)
    live = await _advisory_count(lambda: count_live_validators(session, now=now))
    shadow = await _advisory_count(
        lambda: count_shadow_records_in_window(session, window_start=window)
    )
    return EffectiveEmissionEligibilitySettings(
        revision=policy.revision,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=settings,
        checksum=policy.checksum,
        source="revision" if policy.source == "revision" else "default",
        max_age_seconds=ttl_seconds,
        default=DEFAULT_SETTINGS,
        current_window_start=window,
        next_window_start=next_window_start(
            now, window_seconds=settings.activation_window_seconds
        ),
        live_validator_count=live,
        shadow_excluded_count=shadow,
    )


@router.get(
    "/emission-eligibility", response_model=AdminEmissionEligibilitySettingsResponse
)
async def get_settings(
    request: Request, _admin: AdminDep, session: SessionDep
) -> AdminEmissionEligibilitySettingsResponse:
    now = datetime.now(UTC)
    latest = await latest_eligibility_settings_revision(session)
    history = await list_eligibility_settings_revisions(session)
    shadow = await list_shadow_records(session, limit=_SHADOW_PREVIEW_LIMIT)
    return AdminEmissionEligibilitySettingsResponse(
        current=_revision(latest) if latest is not None else None,
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=await _effective(
            session, latest, ttl_seconds=_resolver(request).ttl_seconds, now=now
        ),
        confirmation_phrase=CONFIRMATION,
        recent_shadow_records=[_shadow_record(row) for row in shadow],
    )


@router.post(
    "/emission-eligibility", response_model=EmissionEligibilitySettingsRevision
)
async def create_settings_revision(
    request: Request,
    payload: AdminEmissionEligibilitySettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> EmissionEligibilitySettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    latest = await latest_eligibility_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "emission eligibility settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_eligibility_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=payload.settings.model_dump(mode="json"),
            checksum=eligibility_checksum(payload.settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "emission eligibility settings changed concurrently; refresh and retry"
            ),
        ) from error
    await session.refresh(row)
    _resolver(request).invalidate()
    logger.warning(
        "emission eligibility enforcement set to %s by %s (revision %d): %s",
        payload.settings.enforcement,
        row.actor,
        row.revision,
        row.reason,
    )
    return _revision(row)


@router.get(
    "/agents/{agent_id}/emission-eligibility",
    response_model=AdminAgentEmissionEligibilityResponse,
)
async def get_agent_eligibility(
    request: Request,
    agent_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
    shadow_limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> AdminAgentEmissionEligibilityResponse:
    """One exact artifact's eligibility record, plus why the fold sees it or not.

    ``in_ledger`` is read from the same ``list_eligible_ledger`` the validator
    reads. ``in_ledger`` false alongside a terminal review means the hold is
    somewhere else entirely (``agents.status``, the ranked-run floor, or a
    rollout version pin) -- which is the answer a miner appeal usually needs and
    the one an operator otherwise has to guess at.
    """
    now = datetime.now(UTC)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    latest = await latest_eligibility_settings_revision(session)
    policy = policy_from_row(latest)
    postures = await load_review_postures(session, [agent_id])
    ledger_rows = await list_eligible_ledger(
        session, include_fingerprints=False, include_details=False
    )
    ledger_row = next((row for row in ledger_rows if row.agent_id == agent_id), None)
    eligibility = classify(
        agent_id=agent_id,
        artifact_sha256=(ledger_row.sha256 if ledger_row is not None else agent.sha256),
        bench_version=(ledger_row.bench_version if ledger_row is not None else None),
        posture=postures.get(agent_id) or AgentReviewPosture(agent_id=agent_id),
        policy=policy,
        now=now,
    )
    shadow = await list_shadow_records(session, agent_id=agent_id, limit=shadow_limit)
    return AdminAgentEmissionEligibilityResponse(
        eligibility=eligibility,
        in_ledger=ledger_row is not None,
        effective=await _effective(
            session, latest, ttl_seconds=_resolver(request).ttl_seconds, now=now
        ),
        shadow_records=[_shadow_record(row) for row in shadow],
    )

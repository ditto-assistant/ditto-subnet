"""Audited Platform control plane for Bench v9 confirmation bundles."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.confirmation_bundles import (
    AblationEvidence,
    AdminConfirmationBundleListResponse,
    AdminConfirmationBundleRetestRequest,
    AdminConfirmationBundleRetestResponse,
    AdminConfirmationBundleSettingsRequest,
    AdminConfirmationBundleSettingsResponse,
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleState,
    ConfirmationBundleSubjectView,
    ConfirmationBundleTicketView,
    ConfirmationBundleView,
    ConfirmationDailyBudgetView,
    ConfirmationDimension,
    ConfirmationDimensionEvidenceView,
    ConfirmationEvidenceRoot,
    ConfirmationResultStatus,
    ConfirmationShadowCalibrationView,
    EffectiveConfirmationBundleSettings,
    LongMemEvidence,
    PrepareRejectionCode,
)
from ditto.api_server.confirmation_candidate_reconciliation import (
    reconcile_confirmation_candidates,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    ConfirmationBudgetDay,
    ConfirmationBundle,
)
from ditto.db.models import (
    ConfirmationBundleSettingsRevision as SettingsRevisionRow,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_bundles import (
    GLOBAL_SCOPE,
    ConfirmationBundlePersistenceError,
    authorize_confirmation_bundle_retest,
    confirmation_budget_day,
    confirmation_bundle_dimensions,
    confirmation_bundle_subjects,
    confirmation_bundle_tickets,
    confirmation_shadow_calibration,
    count_confirmation_bundles,
    insert_confirmation_bundle_settings_revision,
    latest_confirmation_bundle_settings_revision,
    list_confirmation_bundle_settings_revisions,
    list_confirmation_bundles,
)
from ditto.db.queries.confirmation_policy_lock import lock_confirmation_policy

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
DEFAULT_SETTINGS = ConfirmationBundleSettings()


def _checksum(settings: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _settings_revision(
    row: SettingsRevisionRow,
) -> ConfirmationBundleSettingsRevision:
    return ConfirmationBundleSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=ConfirmationBundleSettings.model_validate_json(
            json.dumps(row.settings)
        ),
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _effective_settings(
    latest: SettingsRevisionRow | None,
) -> EffectiveConfirmationBundleSettings:
    settings = (
        ConfirmationBundleSettings.model_validate_json(json.dumps(latest.settings))
        if latest is not None
        else DEFAULT_SETTINGS
    )
    configured = (
        settings.profile_revision is not None and settings.profile_checksum is not None
    )
    return EffectiveConfirmationBundleSettings(
        revision=latest.revision if latest is not None else 0,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=settings,
        checksum=latest.checksum if latest is not None else None,
        source="revision" if latest is not None else "default",
        configured=configured,
        issuance_active=settings.mode != ConfirmationBundleMode.OFF and configured,
    )


def _budget_view(row: ConfirmationBudgetDay | None) -> ConfirmationDailyBudgetView:
    today = datetime.now(UTC).date()
    if row is None:
        return ConfirmationDailyBudgetView(
            utc_day=today,
            revision=0,
            issued_attempts=0,
            outstanding_reserved_microusd=0,
            settled_microusd=0,
        )
    return ConfirmationDailyBudgetView(
        utc_day=row.utc_day,
        revision=row.revision,
        issued_attempts=row.issued_attempts,
        outstanding_reserved_microusd=row.outstanding_reserved_microusd,
        settled_microusd=row.settled_microusd,
    )


async def _shadow_calibration_view(
    session: AsyncSession,
    *,
    now: datetime,
    bench_version: int,
    profile_revision: str | None,
    profile_checksum: str | None,
) -> ConfirmationShadowCalibrationView:
    measured = await confirmation_shadow_calibration(
        session,
        now=now,
        bench_version=bench_version,
        profile_revision=profile_revision,
        profile_checksum=profile_checksum,
    )
    observed_from = measured.observed_from.date() if measured.observed_from else None
    observed_through = (
        measured.observed_through.date() if measured.observed_through else None
    )
    observation_days = (
        (observed_through - observed_from).days + 1
        if observed_from is not None and observed_through is not None
        else 0
    )
    total_cost = measured.base_cost_microusd + measured.bundle_cost_microusd
    return ConfirmationShadowCalibrationView(
        observed_from_utc_day=observed_from,
        observed_through_utc_day=observed_through,
        observation_days=observation_days,
        confirmation_profile_revision=profile_revision,
        confirmation_profile_checksum=profile_checksum,
        base_run_count=measured.base_run_count,
        measured_base_cost_microusd=(
            (measured.base_cost_microusd + measured.base_run_count // 2)
            // measured.base_run_count
            if measured.base_run_count
            else None
        ),
        confirmation_bundle_count=measured.bundle_count,
        measured_bundle_cost_microusd=(
            (measured.bundle_cost_microusd + measured.bundle_count // 2)
            // measured.bundle_count
            if measured.bundle_count
            else None
        ),
        bench_version=bench_version,
        # Completed means "produced verified evidence". Superseded and failed
        # generations are reported on their own axes: folding them into the
        # completed count is what let a lane with zero completions read as a
        # populated window whose promotion rate happened to be zero.
        completed_bundle_count=measured.completed_bundle_count,
        superseded_bundle_count=measured.superseded_bundle_count,
        failed_bundle_count=measured.failed_bundle_count,
        qualified_bundle_count=measured.qualified_bundle_count,
        promotion_rate_bps=(
            (
                measured.qualified_bundle_count * 10_000
                + measured.completed_bundle_count // 2
            )
            // measured.completed_bundle_count
            if measured.completed_bundle_count
            else None
        ),
        projected_daily_spend_microusd=(
            (total_cost + observation_days // 2) // observation_days
            if observation_days
            else None
        ),
        epoch_duration_seconds=None,
        projected_epoch_spend_microusd=None,
        epoch_projection_unavailable_reason=(
            f"Bench v{bench_version} has no configured epoch duration; "
            "no projection was guessed."
        ),
    )


async def _bundle_view(
    session: AsyncSession, bundle: ConfirmationBundle
) -> ConfirmationBundleView:
    subjects = await confirmation_bundle_subjects(session, bundle_id=bundle.bundle_id)
    dimensions = await confirmation_bundle_dimensions(
        session, bundle_id=bundle.bundle_id
    )
    tickets = await confirmation_bundle_tickets(session, bundle_id=bundle.bundle_id)
    return ConfirmationBundleView(
        bundle_id=bundle.bundle_id,
        artifact_sha256=bundle.artifact_sha256,
        bench_version=bundle.bench_version,
        profile_revision=bundle.profile_revision,
        profile_checksum=bundle.profile_checksum,
        retest_generation=bundle.retest_generation,
        generation_reason=cast(
            Literal["initial", "operator_retest", "settings_supersession"],
            bundle.generation_reason,
        ),
        source_bundle_id=bundle.source_bundle_id,
        state=ConfirmationBundleState(bundle.state),
        settings_revision=bundle.settings_revision,
        settings_checksum=bundle.settings_checksum,
        qualification_status=cast(
            Literal["qualified", "unqualified"] | None,
            bundle.qualification_status,
        ),
        completion_mode=(
            ConfirmationBundleMode(bundle.completion_mode)
            if bundle.completion_mode is not None
            else None
        ),
        completion_ticket_id=bundle.completion_ticket_id,
        evidence_sha256=bundle.evidence_sha256,
        reporter_hotkey=bundle.reporter_hotkey,
        bundle_signature=bundle.bundle_signature,
        evidence_root=(
            ConfirmationEvidenceRoot.model_validate(bundle.evidence_root)
            if bundle.evidence_root is not None
            else None
        ),
        verified_at=bundle.verified_at,
        completed_at=bundle.completed_at,
        created_at=bundle.created_at,
        updated_at=bundle.updated_at,
        subjects=[
            ConfirmationBundleSubjectView(
                agent_id=row.agent_id,
                bench_version=row.bench_version,
                artifact_sha256=row.artifact_sha256,
                result_status=ConfirmationResultStatus(row.result_status),
                base_evidence_sha256=row.base_evidence_sha256,
                base_quality_micros=row.base_quality_micros,
                base_stderr_micros=row.base_stderr_micros,
                base_model_factor_bps=row.base_model_factor_bps,
                base_tool_factor_bps=row.base_tool_factor_bps,
                full_quality_micros=row.full_quality_micros,
                full_stderr_micros=row.full_stderr_micros,
                semantic_factor_bps=row.semantic_factor_bps,
                applied_factor_bps=row.applied_factor_bps,
                full_effective_micros=row.full_effective_micros,
                bundle_id=row.bundle_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in subjects
        ],
        dimensions=[
            ConfirmationDimensionEvidenceView(
                dimension=ConfirmationDimension(row.dimension),
                status=cast(Literal["completed", "not_run", "unavailable"], row.status),
                evidence_sha256=row.evidence_sha256,
                request_count=row.request_count,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                provider_cost_microusd=row.provider_cost_microusd,
                latency_ms=row.latency_ms,
                synthetic=row.synthetic,
                evidence=(
                    LongMemEvidence.model_validate(row.evidence)
                    if row.dimension == ConfirmationDimension.LONGMEMEVAL.value
                    else AblationEvidence.model_validate(row.evidence)
                ),
                created_at=row.created_at,
            )
            for row in dimensions
        ],
        tickets=[
            ConfirmationBundleTicketView(
                ticket_id=row.ticket_id,
                validator_hotkey=row.validator_hotkey,
                slot_id=row.slot_id,
                status=cast(Literal["issued", "scored", "expired"], row.status),
                attempt=row.attempt,
                issued_at=row.issued_at,
                deadline=row.deadline,
                failure_reason=row.failure_reason,
                failure_class=row.failure_class,
                failure_stage=row.failure_stage,
                failed_at=row.failed_at,
                prepare_rejection=cast(
                    PrepareRejectionCode | None, row.prepare_rejection
                ),
                prepare_rejected_at=row.prepare_rejected_at,
            )
            for row in tickets
        ],
    )


@router.get(
    "/admin/confirmation-bundle-settings",
    response_model=AdminConfirmationBundleSettingsResponse,
)
async def get_confirmation_bundle_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminConfirmationBundleSettingsResponse:
    latest = await latest_confirmation_bundle_settings_revision(session)
    history = await list_confirmation_bundle_settings_revisions(session)
    return AdminConfirmationBundleSettingsResponse(
        current=[_settings_revision(latest)] if latest is not None else [],
        history=[_settings_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=_effective_settings(latest),
    )


@router.post(
    "/admin/confirmation-bundle-settings",
    response_model=ConfirmationBundleSettingsRevision,
)
async def create_confirmation_bundle_settings_revision(
    payload: AdminConfirmationBundleSettingsRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> ConfirmationBundleSettingsRevision:
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(status_code=422, detail="scope must be '*'")
    expected_confirmation = (
        f"APPLY V9 CONFIRMATION MODE {payload.settings.mode.value.upper()}"
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    # A policy revision and a costly validator claim must have one total order.
    # Without this transaction lock an OFF revision can commit while a claim
    # concurrently reserves against the prior active revision.
    await lock_confirmation_policy(session)
    latest = await latest_confirmation_bundle_settings_revision(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "confirmation bundle settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_confirmation_bundle_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=payload.settings.model_dump(mode="json"),
            checksum=_checksum(payload.settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        # The append-only revision is visible in this transaction, so changing
        # OFF/SHADOW/ENFORCE or installing an exact profile immediately
        # reconciles already-finalized candidates on the live benchmark. This
        # creates/reuses only metadata; reservation and execution remain
        # validator-claim work.
        await reconcile_confirmation_candidates(
            session,
            bench_version=await active_bench_version(session),
            verification_profiles=getattr(
                request.app.state, "confirmation_verification_profiles", {}
            ),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "confirmation bundle settings changed concurrently; refresh and retry"
            ),
        ) from error
    await session.refresh(row)
    return _settings_revision(row)


@router.get(
    "/admin/confirmation-bundles",
    response_model=AdminConfirmationBundleListResponse,
)
async def get_confirmation_bundles(
    _admin: AdminDep,
    session: SessionDep,
    state: Annotated[ConfirmationBundleState | None, Query()] = None,
    generation: Annotated[Literal["active", "all"], Query()] = "active",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminConfirmationBundleListResponse:
    """List current-era confirmation evidence unless history is requested.

    Bundles are immutable evidence records, so a historical bundle remains
    available by exact id for audit and manual retest.  The generic worklist
    instead follows the benchmark currently in force (plus any newer rollout
    work); ``generation=all`` is the explicit way to inspect the full lineage.
    """
    active_version = await active_bench_version(session)
    minimum_bench_version = active_version if generation == "active" else None
    rows = await list_confirmation_bundles(
        session,
        state=state.value if state is not None else None,
        minimum_bench_version=minimum_bench_version,
        limit=limit,
        offset=offset,
    )
    count = await count_confirmation_bundles(
        session,
        state=state.value if state is not None else None,
        minimum_bench_version=minimum_bench_version,
    )
    now = datetime.now(UTC)
    today = now.date()
    budget = await confirmation_budget_day(session, utc_day=today)
    latest_settings = await latest_confirmation_bundle_settings_revision(session)
    effective_settings = _effective_settings(latest_settings).settings
    return AdminConfirmationBundleListResponse(
        items=[await _bundle_view(session, row) for row in rows],
        count=count,
        generation=generation,
        active_bench_version=active_version,
        budget=_budget_view(budget),
        shadow_calibration=await _shadow_calibration_view(
            session,
            now=now,
            bench_version=active_version,
            profile_revision=effective_settings.profile_revision,
            profile_checksum=effective_settings.profile_checksum,
        ),
    )


@router.get(
    "/admin/confirmation-bundles/{bundle_id}", response_model=ConfirmationBundleView
)
async def get_confirmation_bundle(
    bundle_id: UUID, _admin: AdminDep, session: SessionDep
) -> ConfirmationBundleView:
    bundle = await session.get(ConfirmationBundle, bundle_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="confirmation bundle not found")
    return await _bundle_view(session, bundle)


@router.post(
    "/admin/confirmation-bundles/{bundle_id}/authorize-retest",
    response_model=AdminConfirmationBundleRetestResponse,
)
async def authorize_confirmation_retest(
    bundle_id: UUID,
    payload: AdminConfirmationBundleRetestRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminConfirmationBundleRetestResponse:
    expected_confirmation = "AUTHORIZE CONFIRMATION BUNDLE RETEST"
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    actor = payload.actor.strip()
    reason = payload.reason.strip()
    try:
        result = await authorize_confirmation_bundle_retest(
            session,
            source_bundle_id=bundle_id,
            authorization_id=payload.request_id,
            expected_generation=payload.expected_generation,
            actor=actor,
            reason=reason,
        )
        await session.commit()
    except ConfirmationBundlePersistenceError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="confirmation generation changed concurrently; refresh and retry",
        ) from error
    await session.refresh(result.bundle)
    return AdminConfirmationBundleRetestResponse(
        authorization_id=result.authorization.authorization_id,
        superseded_bundle_id=result.superseded_bundle.bundle_id,
        bundle=await _bundle_view(session, result.bundle),
        replayed=result.replayed,
    )


__all__ = ["DEFAULT_SETTINGS", "router"]

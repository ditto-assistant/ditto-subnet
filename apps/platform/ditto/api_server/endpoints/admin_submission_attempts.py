"""Audited activation, labeled shadow replay, and source-safe attempt appeals."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_attempts import (
    AttemptAppealRequest,
    AttemptAppealResponse,
    AttemptCalibrationResponse,
    AttemptControlSettings,
    AttemptGuidance,
    AttemptPolicyResponse,
    AttemptPolicyRevision,
    AttemptRecordResponse,
    AttemptReplayReport,
    AttemptReplayRequest,
    AttemptReplayRow,
    AttemptSettingsRequest,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.submission_attempts import settings_digest, utc
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    SubmissionAttempt,
    SubmissionAttemptAppeal,
    SubmissionAttemptCalibration,
    SubmissionAttemptSettingsRevision,
)
from ditto.db.queries.submission_attempts import attempt_settings, compare_attempt

router = APIRouter(prefix="/admin/submission-attempts", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: SubmissionAttemptSettingsRevision) -> AttemptPolicyRevision:
    return AttemptPolicyRevision.model_validate(
        {
            "revision": row.revision,
            "parent_revision": row.parent_revision,
            "settings": row.settings,
            "calibration_id": row.calibration_id,
            "actor": row.actor,
            "reason": row.reason,
            "created_at": row.created_at,
        }
    )


@router.get("", response_model=AttemptPolicyResponse)
async def get_policy(_admin: AdminDep, session: SessionDep) -> AttemptPolicyResponse:
    rows = list(
        await session.scalars(
            select(SubmissionAttemptSettingsRevision)
            .order_by(SubmissionAttemptSettingsRevision.revision.desc())
            .limit(100)
        )
    )
    current = (
        _revision(rows[0])
        if rows
        else AttemptPolicyRevision(
            revision=0,
            parent_revision=0,
            settings=AttemptControlSettings(),
            actor="platform",
            reason="Built-in shadow collection; no new enforcement",
        )
    )
    effective, _ = await attempt_settings(session)
    return AttemptPolicyResponse(
        current=current,
        effective_settings=effective,
        enforcement_blocked_reason=(
            "Calibration no longer matches this classifier/reference build."
            if current.settings.mode == "enforce" and effective.mode != "enforce"
            else None
        ),
        history=[_revision(row) for row in rows],
    )


@router.post("", response_model=AttemptPolicyRevision)
async def set_policy(
    payload: AttemptSettingsRequest, _admin: AdminDep, session: SessionDep
) -> AttemptPolicyRevision:
    _, revision = await attempt_settings(session)
    if payload.expected_revision != revision:
        raise HTTPException(409, "Attempt policy changed; refresh before applying.")
    if (
        payload.confirmation
        != f"SET SUBMISSION ATTEMPT MODE {payload.settings.mode.upper()}"
    ):
        raise HTTPException(409, "Exact mode confirmation is required.")
    if payload.settings.mode == "enforce":
        calibration = (
            await session.get(SubmissionAttemptCalibration, payload.calibration_id)
            if payload.calibration_id is not None
            else None
        )
        if (
            calibration is None
            or calibration.settings_digest != settings_digest(payload.settings)
            or not calibration.report.get("eligible_for_enforcement")
            or utc(calibration.created_at) < datetime.now(UTC) - timedelta(days=7)
        ):
            raise HTTPException(
                409,
                "Enforcement requires a reviewed, compatible shadow replay from "
                "the last seven days with complete coverage and no false throttles.",
            )
    row = SubmissionAttemptSettingsRevision(
        parent_revision=revision,
        settings=payload.settings.model_dump(mode="json"),
        calibration_id=payload.calibration_id,
        actor=payload.actor,
        reason=payload.reason,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            409, "Attempt policy changed concurrently; refresh before applying."
        ) from error
    await session.refresh(row)
    return _revision(row)


@router.post("/replay", response_model=AttemptReplayReport)
async def shadow_replay(
    payload: AttemptReplayRequest, _admin: AdminDep, session: SessionDep
) -> AttemptReplayReport:
    from ditto.api_server.attestation import expected_netuid

    rows: list[AttemptReplayRow] = []
    for case in payload.cases:
        identity = (
            await session.execute(
                select(
                    Agent.miner_hotkey,
                    Agent.created_at,
                    EvaluationPayment.miner_coldkey,
                    SubmissionAttempt.profile,
                    SubmissionAttempt.guidance,
                )
                .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
                .outerjoin(
                    SubmissionAttempt, SubmissionAttempt.agent_id == Agent.agent_id
                )
                .where(Agent.agent_id == case.agent_id)
            )
        ).one_or_none()
        if identity is None:
            raise HTTPException(404, f"No paid submission exists for {case.agent_id}.")
        hotkey, created_at, coldkey, profile, recorded = identity
        observed = AttemptGuidance.model_validate(recorded) if recorded else None
        guidance = await compare_attempt(
            session,
            profile=profile,
            hotkey=hotkey,
            coldkey=coldkey,
            netuid=expected_netuid(),
            settings=payload.settings,
            revision=0,
            now=(
                observed.evaluated_at
                if observed and observed.evaluated_at
                else created_at
            ),
            require_compatible_history=True,
            include_reservations=False,
            replay_agent_id=case.agent_id,
        )
        if (
            observed is not None
            and observed.evaluated_at is not None
            and (
                observed.completed_low_information_attempts
                != guidance.completed_low_information_attempts
                or observed.reserved_low_information_attempts
                != guidance.reserved_low_information_attempts
                or observed.fast_repairs_remaining != guidance.fast_repairs_remaining
            )
        ):
            guidance = guidance.model_copy(
                update={
                    "classification": "inconclusive",
                    "retry_at": None,
                    "reason": "Historical tentative capacity or feedback cannot "
                    "be reconstructed exactly.",
                }
            )
        rows.append(
            AttemptReplayRow(
                agent_id=case.agent_id,
                guidance=guidance,
                expected_classification=case.expected_classification,
                expected_throttled=case.expected_throttled,
                would_throttle=guidance.retry_at is not None,
            )
        )
    coverage: Counter[str] = Counter(str(row.expected_classification) for row in rows)
    false_throttles = sum(
        row.would_throttle and not row.expected_throttled for row in rows
    )
    false_allows = sum(
        not row.would_throttle and row.expected_throttled for row in rows
    )
    mismatches = sum(
        row.guidance.classification != row.expected_classification for row in rows
    )
    inconclusive = sum(row.guidance.classification == "inconclusive" for row in rows)
    delays = sum(row.would_throttle for row in rows)
    required = {
        "infrastructure_retry",
        "packaging_only_repair",
        "small_source_delta",
        "material_new_work",
    }
    report = AttemptReplayReport(
        calibration_id=uuid4(),
        settings_digest=settings_digest(payload.settings),
        case_count=len(rows),
        false_throttles=false_throttles,
        false_allows=false_allows,
        classification_mismatches=mismatches,
        inconclusive_count=inconclusive,
        proposed_delays=delays,
        immediate_admissions_deferred=delays,
        immediate_admission_deferral_ratio=delays / len(rows),
        eligible_for_enforcement=len(rows) >= 8
        and required.issubset(coverage)
        and delays > 0
        and false_throttles == 0
        and false_allows == 0
        and mismatches == 0
        and inconclusive == 0,
        coverage=dict(coverage),
        rows=rows,
    )
    session.add(
        SubmissionAttemptCalibration(
            calibration_id=report.calibration_id,
            settings_digest=report.settings_digest,
            report=report.model_dump(mode="json"),
            actor=payload.actor,
            reason=payload.reason,
        )
    )
    await session.commit()
    return report


@router.get("/replay/{calibration_id}", response_model=AttemptCalibrationResponse)
async def read_calibration(
    calibration_id: UUID, _admin: AdminDep, session: SessionDep
) -> AttemptCalibrationResponse:
    row = await session.get(SubmissionAttemptCalibration, calibration_id)
    if row is None:
        raise HTTPException(404, "No shadow replay exists for this calibration.")
    return AttemptCalibrationResponse(
        report=AttemptReplayReport.model_validate(row.report),
        actor=row.actor,
        reason=row.reason,
        created_at=row.created_at,
    )


def _appeal(row: SubmissionAttemptAppeal) -> AttemptAppealResponse:
    return AttemptAppealResponse(
        appeal_id=row.appeal_id,
        policy_revision=row.policy_revision,
        actor=row.actor,
        reason=row.reason,
        created_at=row.created_at,
    )


@router.get("/{agent_id}", response_model=AttemptRecordResponse)
async def read_attempt(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AttemptRecordResponse:
    row = await session.get(SubmissionAttempt, agent_id)
    if row is None:
        raise HTTPException(
            404, "No source-comparison record exists for this submission."
        )
    appeals = await session.scalars(
        select(SubmissionAttemptAppeal)
        .where(SubmissionAttemptAppeal.agent_id == agent_id)
        .order_by(SubmissionAttemptAppeal.created_at.desc())
    )
    return AttemptRecordResponse(
        guidance=AttemptGuidance.model_validate(row.guidance),
        appeals=[_appeal(appeal) for appeal in appeals],
    )


@router.post("/appeal", response_model=AttemptAppealResponse)
async def grant_appeal(
    payload: AttemptAppealRequest, _admin: AdminDep, session: SessionDep
) -> AttemptAppealResponse:
    _, revision = await attempt_settings(session)
    if payload.expected_policy_revision != revision:
        raise HTTPException(
            409, "Attempt policy changed; refresh before granting the appeal."
        )
    if payload.confirmation != f"ALLOW SUBMISSION RETRY {payload.agent_id}":
        raise HTTPException(409, "Exact submission confirmation is required.")
    if await session.get(SubmissionAttempt, payload.agent_id) is None:
        raise HTTPException(
            404, "No source-comparison record exists for this submission."
        )
    existing = await session.scalar(
        select(SubmissionAttemptAppeal).where(
            SubmissionAttemptAppeal.agent_id == payload.agent_id,
            SubmissionAttemptAppeal.policy_revision == revision,
        )
    )
    if existing is not None:
        return _appeal(existing)
    row = SubmissionAttemptAppeal(
        appeal_id=uuid4(),
        agent_id=payload.agent_id,
        policy_revision=revision,
        actor=payload.actor,
        reason=payload.reason,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            409, "Appeal changed concurrently; refresh before retrying."
        ) from error
    await session.refresh(row)
    return _appeal(row)

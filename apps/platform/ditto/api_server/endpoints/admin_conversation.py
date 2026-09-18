"""Budget-fenced top-five conversation lane. Reports never write score rows."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.conversation import (
    ConversationClaim,
    ConversationObservation,
    ConversationObservations,
    ConversationReport,
    ConversationResultRequest,
    ConversationSettingsRequest,
)
from ditto.api_models.submission_settings import AdminSubmissionSettingsRequest
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    AgentStatus,
    ConversationAssessment,
    ConversationSettingsRevision,
)
from ditto.db.queries.submission_settings import effective_submission_settings
from ditto_screening_protocol.conversation import (
    INSTRUMENT,
    JUDGE_MODEL,
    proposed_quality_micros,
)
from ditto_screening_protocol.conversation_story import story, story_digest

router = APIRouter(prefix="/admin/conversation-assessments", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
DAILY_BUDGET_MICROUSD = 150_000_000
RUN_RESERVATION_MICROUSD = 30_000_000


async def _settings(session: AsyncSession) -> ConversationSettingsRevision | None:
    return await session.scalar(
        select(ConversationSettingsRevision)
        .order_by(ConversationSettingsRevision.revision.desc())
        .limit(1)
    )


async def _enabled(request: Request, session: AsyncSession) -> bool:
    settings = await _settings(session)
    return (
        settings.enabled
        if settings
        else request.app.state.config.conversation_shadow_enabled
    )


@router.post("/settings", response_model=ConversationObservations)
async def set_settings(
    request: Request,
    payload: ConversationSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ConversationObservations:
    await session.execute(text("SELECT pg_advisory_xact_lock(761882019)"))
    current = await _settings(session)
    revision = current.revision if current else 0
    if revision != payload.expected_revision:
        raise HTTPException(409, "conversation settings revision changed")
    session.add(
        ConversationSettingsRevision(
            revision=revision + 1,
            enabled=payload.mode == "shadow",
            actor=payload.actor,
            reason=payload.reason,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return await observations(request, None, session)


async def _reserved(session: AsyncSession, now: datetime) -> int:
    return int(
        await session.scalar(
            select(
                func.coalesce(func.sum(ConversationAssessment.reserved_microusd), 0)
            ).where(ConversationAssessment.created_at >= now - timedelta(days=1))
        )
        or 0
    )


def _observation(row: ConversationAssessment, now: datetime) -> ConversationObservation:
    report = ConversationReport.model_validate(row.report) if row.report else None
    score = report.conversation_micros() if report else None
    return ConversationObservation(
        assessment_id=row.assessment_id,
        agent_id=row.agent_id,
        artifact_sha256=row.artifact_sha256,
        bench_version=row.bench_version,
        status=report.status
        if report
        else ("expired" if row.expires_at <= now else "leased"),
        created_at=row.created_at,
        expires_at=row.expires_at,
        base_quality_micros=row.base_quality_micros,
        conversation_micros=score,
        proposed_quality_micros=proposed_quality_micros(row.base_quality_micros, score)
        if score is not None
        else None,
        reserved_microusd=row.reserved_microusd,
        spent_microusd=report.spent_microusd + report.harness_usage.spent_microusd
        if report
        and not report.unmetered
        and report.harness_usage
        and not report.harness_usage.unmetered
        and not report.harness_usage.cost_is_upper_bound
        and not report.judge_cost_is_upper_bound
        else None,
        error_code=report.error_code if report else None,
    )


@router.get("", response_model=ConversationObservations)
async def observations(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationObservations:
    now = datetime.now(UTC)
    settings = await _settings(session)
    fee = await effective_submission_settings(
        session, default_payment_address=request.app.state.config.upload_payment_address
    )
    rows = list(
        await session.scalars(
            select(ConversationAssessment)
            .order_by(
                ConversationAssessment.created_at.desc(),
                ConversationAssessment.assessment_id,
            )
            .limit(limit)
        )
    )
    return ConversationObservations(
        mode="shadow" if await _enabled(request, session) else "off",
        instrument=INSTRUMENT,
        judge_model=JUDGE_MODEL,
        daily_budget_microusd=DAILY_BUDGET_MICROUSD,
        reserved_last_day_microusd=await _reserved(session, now),
        current_submission_fee_rao=fee.fee_amount_rao,
        fee_change_request=AdminSubmissionSettingsRequest(
            expected_revision=fee.revision,
            cooldown_seconds=fee.cooldown_seconds,
            fee_amount_rao=200_000_000,
            reason="Fund top-five conversational continuity assessments",
            actor="conversation-rollout",
            confirmation=(
                f"SET SUBMISSION COOLDOWN {fee.cooldown_seconds} SECONDS "
                "FEE 200000000 RAO"
            ),
        ),
        items=[_observation(row, now) for row in rows],
        settings_revision=settings.revision if settings else 0,
        settings_actor=settings.actor if settings else None,
        settings_reason=settings.reason if settings else None,
        settings_updated_at=settings.created_at if settings else None,
    )


@router.post("/claim", response_model=ConversationClaim | None)
async def claim(
    request: Request, _admin: AdminDep, session: SessionDep
) -> ConversationClaim | None:
    return await claim_assessment(request, session)


async def claim_assessment(
    request: Request, session: AsyncSession, worker_hotkey: str | None = None
) -> ConversationClaim | None:
    if not await _enabled(request, session):
        return None
    # Same finalized, owner-deduplicated ordering seen by users, not a new SQL
    # approximation of fifth place. Imported lazily to avoid router cycles.
    from ditto.api_server.endpoints.public import build_public_leaderboard

    board = await build_public_leaderboard(request, Response(), session)
    candidates = sorted(
        (
            entry
            for entry in board.entries
            if entry.finalized
            and entry.rank is not None
            and entry.rank <= 5
            and entry.bench_version is not None
            and entry.bench_version >= 9
        ),
        key=lambda entry: entry.rank or 0,
    )
    now = datetime.now(UTC)
    # Serialize all admission cost reservations, including concurrent workers.
    await session.execute(text("SELECT pg_advisory_xact_lock(761882019)"))
    # A single global paid assessment leaves capacity for the primary screener.
    # Recheck the kill switch under the same lock used by settings writes.
    if not await _enabled(request, session):
        return None
    active = await session.scalar(
        select(ConversationAssessment.assessment_id)
        .where(
            ConversationAssessment.report.is_(None),
            ConversationAssessment.expires_at > now,
        )
        .limit(1)
    )
    if active is not None:
        return None
    if await _reserved(session, now) + RUN_RESERVATION_MICROUSD > DAILY_BUDGET_MICROUSD:
        return None
    for entry in candidates:
        agent = await session.get(Agent, entry.agent_id)
        if (
            agent is None
            or agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE)
            or not agent.screened_image_sha256
            or not agent.screened_image_id
            or not agent.screened_image_upload_id
            or not agent.screened_image_size_bytes
            or not agent.screened_image_verified_at
        ):
            continue
        existing = await session.scalar(
            select(ConversationAssessment.assessment_id).where(
                ConversationAssessment.agent_id == agent.agent_id,
                ConversationAssessment.artifact_sha256 == agent.sha256,
                ConversationAssessment.bench_version == entry.bench_version,
                ConversationAssessment.instrument == INSTRUMENT,
            )
        )
        if existing is not None:
            continue  # Expired/failed attempts are not automatic billed retries.
        if not 0 <= entry.official_composite <= 1:
            continue  # Legacy efficiency-adjusted values cannot be base quality.
        row = ConversationAssessment(
            assessment_id=uuid4(),
            agent_id=agent.agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256,
            bench_version=entry.bench_version,
            instrument=INSTRUMENT,
            seed=secrets.token_hex(32),
            lease_token=uuid4(),
            created_at=now,
            expires_at=now + timedelta(minutes=65),
            base_quality_micros=round(entry.official_composite * 1_000_000),
            reserved_microusd=RUN_RESERVATION_MICROUSD,
            worker_hotkey=worker_hotkey,
        )
        session.add(row)
        await session.commit()
        return ConversationClaim(
            assessment_id=row.assessment_id,
            agent_id=row.agent_id,
            artifact_sha256=row.artifact_sha256,
            screened_image_sha256=row.screened_image_sha256,
            bench_version=row.bench_version,
            seed=row.seed,
            lease_token=row.lease_token,
            expires_at=row.expires_at,
        )
    return None


@router.post("/{assessment_id}/result", response_model=ConversationObservation)
async def submit_result(
    assessment_id: UUID,
    payload: ConversationResultRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ConversationObservation:
    row = await session.scalar(
        select(ConversationAssessment)
        .where(ConversationAssessment.assessment_id == assessment_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "unknown conversation assessment")
    report = payload.report
    if payload.lease_token != row.lease_token or (
        report.assessment_id,
        report.agent_id,
        report.artifact_sha256,
        report.screened_image_sha256,
        report.bench_version,
        report.instrument,
        report.story_sha256,
        report.reserved_microusd,
    ) != (
        row.assessment_id,
        row.agent_id,
        row.artifact_sha256,
        row.screened_image_sha256,
        row.bench_version,
        row.instrument,
        story_digest(row.seed),
        25_000_000,
    ):
        raise HTTPException(409, "conversation identity mismatch")
    expected = story(row.seed)
    if any(
        exchange.user != expected[i].user for i, exchange in enumerate(report.exchanges)
    ):
        raise HTTPException(409, "conversation schedule mismatch")
    document = report.model_dump(mode="json")
    now = datetime.now(UTC)
    if row.report is not None:
        if row.report != document:
            raise HTTPException(409, "conversation result is immutable")
        return _observation(row, now)
    if row.expires_at <= now:
        raise HTTPException(409, "conversation lease expired")
    row.report = document
    await session.commit()
    return _observation(row, now)


@router.get("/{assessment_id}/report", response_model=ConversationReport | None)
async def get_report(
    assessment_id: UUID, _admin: AdminDep, session: SessionDep
) -> ConversationReport | None:
    row = await session.get(ConversationAssessment, assessment_id)
    if row is None:
        raise HTTPException(404, "unknown conversation assessment")
    return ConversationReport.model_validate(row.report) if row.report else None

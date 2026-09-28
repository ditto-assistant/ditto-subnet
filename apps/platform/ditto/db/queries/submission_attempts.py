"""Payment-rooted attempt history, shadow evidence, and audited appeals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_attempts import AttemptControlSettings, AttemptGuidance
from ditto.api_server.submission_attempts import (
    REPAIR_REASONS,
    PriorAttempt,
    decide_attempt,
    settings_digest,
    utc,
)
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    Score,
    ScreeningAttempt,
    SubmissionAttempt,
    SubmissionAttemptAppeal,
    SubmissionAttemptCalibration,
    SubmissionAttemptSettingsRevision,
    UploadAdmissionReservation,
    ValidatorTicket,
)
from ditto.db.queries.attestation import signed_coldkey_peers
from ditto.db.queries.submission_settings import UPLOAD_ADMISSION_BLOCK_TTL


async def attempt_settings(session: AsyncSession) -> tuple[AttemptControlSettings, int]:
    row = await session.scalar(
        select(SubmissionAttemptSettingsRevision)
        .order_by(SubmissionAttemptSettingsRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        return AttemptControlSettings(), 0
    settings = AttemptControlSettings.model_validate(row.settings)
    if settings.mode == "enforce":
        calibration = (
            await session.get(SubmissionAttemptCalibration, row.calibration_id)
            if row.calibration_id is not None
            else None
        )
        if (
            calibration is None
            or not calibration.report.get("eligible_for_enforcement")
            or calibration.settings_digest != settings_digest(settings)
        ):
            # A classifier/reference refresh invalidates old calibration. Keep
            # collecting shadow evidence rather than using uncalibrated timing.
            settings = settings.model_copy(update={"mode": "shadow"})
    return settings, row.revision


async def owner_scope(
    session: AsyncSession,
    *,
    hotkey: str,
    coldkey: str,
    netuid: int,
    now: datetime,
) -> tuple[set[str], Any]:
    """Only the verified payer and directly, reciprocally signed coldkey links.

    Never expand through historic shared payers, names, source similarity, or
    transitive links. Historical replay applies links at their recorded time.
    """
    if not hotkey or not coldkey:
        raise ValueError("Verified hotkey and payer identities are required")
    linked = await signed_coldkey_peers(session, coldkey=coldkey, netuid=netuid, at=now)
    # Endpoint producers verified each coldkey signature against the paid
    # binding for its hotkey. Hotkey-only links cannot prove historical payers:
    # signing keys can transfer between owners without transferring their budget.
    predicate = EvaluationPayment.miner_coldkey.in_({coldkey, *linked})
    return linked, predicate


async def lock_attempt_owner(
    session: AsyncSession, *, hotkey: str, coldkey: str, netuid: int, now: datetime
) -> None:
    linked, _ = await owner_scope(
        session, hotkey=hotkey, coldkey=coldkey, netuid=netuid, now=now
    )
    # Stable lock order also covers a direct signed link across different payers.
    for identity in sorted(f"coldkey:{key}" for key in {coldkey, *linked}):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 1))"),
            {"identity": identity},
        )


def _history_statement(now: datetime):
    screening = (
        select(ScreeningAttempt.attempt_id)
        .where(
            ScreeningAttempt.agent_id == Agent.agent_id,
            ScreeningAttempt.finished_at <= now,
        )
        .order_by(
            ScreeningAttempt.finished_at.desc(), ScreeningAttempt.attempt_id.desc()
        )
        .limit(1)
        .correlate(Agent)
        .scalar_subquery()
    )
    score_time = (
        select(func.max(Score.created_at))
        .where(Score.agent_id == Agent.agent_id, Score.created_at <= now)
        .group_by(Score.bench_version)
        .having(func.count(func.distinct(Score.validator_hotkey)) >= 3)
        .order_by(func.max(Score.created_at).desc())
        .limit(1)
        .correlate(Agent)
        .scalar_subquery()
    )
    infrastructure_time = (
        select(func.max(ValidatorTicket.failed_at))
        .where(
            ValidatorTicket.agent_id == Agent.agent_id,
            ValidatorTicket.failed_at <= now,
            ValidatorTicket.failure_reason.in_({"infrastructure", "scoring_error"}),
        )
        .correlate(Agent)
        .scalar_subquery()
    )
    return (
        select(
            Agent.agent_id,
            Agent.created_at,
            SubmissionAttempt.profile,
            SubmissionAttempt.lineage_agent_id,
            SubmissionAttempt.classification,
            SubmissionAttempt.fast_repair,
            SubmissionAttempt.guidance,
            ScreeningAttempt.status,
            ScreeningAttempt.reason_code,
            ScreeningAttempt.finished_at,
            score_time.label("score_time"),
            infrastructure_time.label("infrastructure_time"),
        )
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .outerjoin(SubmissionAttempt, SubmissionAttempt.agent_id == Agent.agent_id)
        .outerjoin(ScreeningAttempt, ScreeningAttempt.attempt_id == screening)
        .where(Agent.created_at < now)
    )


async def _read_history(session: AsyncSession, statement) -> list[PriorAttempt]:
    rows = (await session.execute(statement)).all()
    result = []
    for row in rows:
        score_time = row.score_time
        infra = row.infrastructure_time
        infrastructure = infra is not None and (
            row.finished_at is None or infra > row.finished_at
        )
        feedback = score_time or (infra if infrastructure else row.finished_at)
        outcome = (
            "completed"
            if score_time is not None
            else "infrastructure"
            if infrastructure
            else "repairable"
            if row.status == "rejected" and row.reason_code in REPAIR_REASONS
            else "completed"
            if row.status == "rejected"
            else "infrastructure"
            if row.status in {"failed", "expired"}
            else "pending"
        )
        result.append(
            PriorAttempt(
                agent_id=row.agent_id,
                lineage_agent_id=row.lineage_agent_id or row.agent_id,
                profile=row.profile or {},
                classification=row.classification or "inconclusive",
                submitted_at=row.created_at,
                feedback_at=feedback,
                outcome=outcome,
                reason_code=row.reason_code,
                fast_repair=bool(row.fast_repair),
                policy_digest=(row.guidance or {}).get("settings_digest", ""),
            )
        )
    return result


async def compare_attempt(
    session: AsyncSession,
    *,
    profile: dict[str, Any] | None,
    hotkey: str,
    coldkey: str,
    netuid: int,
    settings: AttemptControlSettings,
    revision: int,
    now: datetime | None = None,
    require_compatible_history: bool = False,
    include_reservations: bool = True,
    replay_agent_id: UUID | None = None,
) -> AttemptGuidance:
    current = utc(now or datetime.now(UTC))
    linked, owned = await owner_scope(
        session, hotkey=hotkey, coldkey=coldkey, netuid=netuid, now=current
    )
    statement = _history_statement(current).where(owned)
    history = await _read_history(
        session,
        statement.order_by(Agent.created_at.desc(), Agent.agent_id.desc()).limit(100),
    )
    # Exact runtime equality remains discoverable across the full paid history,
    # including a repack of an old version after many independent new versions.
    if profile is not None:
        history.extend(
            await _read_history(
                session,
                statement.where(
                    SubmissionAttempt.runtime_hash == profile["runtime_hash"]
                )
                .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
                .limit(1),
            )
        )
    history = list({item.agent_id: item for item in history}.values())
    initial = decide_attempt(
        profile, history, settings=settings, revision=revision, now=current
    )
    if replay_agent_id is not None:
        tied = await session.scalar(
            select(Agent.agent_id)
            .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
            .where(
                owned, Agent.created_at == current, Agent.agent_id != replay_agent_id
            )
            .limit(1)
        )
        if tied is not None:
            return initial.model_copy(
                update={
                    "classification": "inconclusive",
                    "retry_at": None,
                    "reason": "Equal submission timestamps cannot establish "
                    "replay order.",
                }
            )
    if initial.lineage_agent_id is None:
        return initial
    lineage = initial.lineage_agent_id
    # At most the configured maximum budget plus one is needed to establish
    # exhaustion. Select this lineage directly; unrelated versions cannot push
    # its usage out of an owner-history page.
    family_statement = statement.where(
        SubmissionAttempt.lineage_agent_id == lineage,
        Agent.created_at >= current - timedelta(seconds=settings.window_seconds),
    ).order_by(Agent.created_at.desc(), Agent.agent_id.desc())
    # Feedback and fast-repair grants cannot be pushed out of a recent page by
    # a stream of unresolved or infrastructure-failed submissions.
    family = await _read_history(session, family_statement.limit(111))
    no_infra = or_(
        statement.selected_columns.infrastructure_time.is_(None),
        statement.selected_columns.infrastructure_time <= ScreeningAttempt.finished_at,
    )
    completed = or_(
        statement.selected_columns.score_time.is_not(None),
        and_(ScreeningAttempt.status == "rejected", no_infra),
    )
    pending = and_(
        statement.selected_columns.score_time.is_(None),
        no_infra,
        or_(
            ScreeningAttempt.status.is_(None),
            ScreeningAttempt.status.not_in({"rejected", "failed", "expired"}),
        ),
    )
    family.extend(
        await _read_history(
            session,
            family_statement.where(
                completed,
            ).limit(111),
        )
    )
    family.extend(
        await _read_history(
            session,
            family_statement.where(
                SubmissionAttempt.fast_repair.is_(True), or_(completed, pending)
            ).limit(11),
        )
    )
    family.extend(
        await _read_history(
            session,
            family_statement.where(
                pending,
                SubmissionAttempt.fast_repair.is_(False),
                SubmissionAttempt.classification.in_(
                    {"small_source_delta", "packaging_only_repair"}
                ),
            ).limit(101),
        )
    )
    anchor = await _read_history(session, statement.where(Agent.agent_id == lineage))
    history = list(
        {item.agent_id: item for item in (*history, *family, *anchor)}.values()
    )
    if require_compatible_history and any(
        item.lineage_agent_id == lineage
        and item.policy_digest != initial.settings_digest
        for item in history
    ):
        return initial.model_copy(
            update={
                "classification": "inconclusive",
                "retry_at": None,
                "reason": "Collect shadow history under the replay settings first.",
            }
        )
    if include_reservations:
        reservations = await session.scalars(
            select(UploadAdmissionReservation).where(
                UploadAdmissionReservation.miner_coldkey.in_({coldkey, *linked}),
                UploadAdmissionReservation.expires_at > current,
                UploadAdmissionReservation.created_at
                > current - UPLOAD_ADMISSION_BLOCK_TTL,
                UploadAdmissionReservation.created_at <= current,
                UploadAdmissionReservation.attempt_context.is_not(None),
            )
        )
        for reservation in reservations:
            context = reservation.attempt_context
            if not context:
                continue
            reserved = AttemptGuidance.model_validate(context["guidance"])
            if reserved.lineage_agent_id == lineage:
                history.append(
                    PriorAttempt(
                        agent_id=reservation.token,
                        lineage_agent_id=lineage,
                        # A reservation consumes tentative capacity only. It is never
                        # evidence that its unsubmitted source is a predecessor.
                        profile={},
                        classification=reserved.classification,
                        submitted_at=reservation.created_at,
                        feedback_at=None,
                        outcome="pending",
                        fast_repair=reserved.fast_repair,
                    )
                )
    result = decide_attempt(
        profile, history, settings=settings, revision=revision, now=current
    )
    if result.reference_agent_id is not None:
        appeal = await session.scalar(
            select(SubmissionAttemptAppeal)
            .where(
                SubmissionAttemptAppeal.agent_id == result.reference_agent_id,
                SubmissionAttemptAppeal.policy_revision == revision,
                SubmissionAttemptAppeal.created_at <= current,
            )
            .limit(1)
        )
        if appeal is not None:
            consumed = await session.scalar(
                select(SubmissionAttempt.agent_id)
                .join(Agent, Agent.agent_id == SubmissionAttempt.agent_id)
                .where(
                    SubmissionAttempt.guidance["appeal_id"].as_string()
                    == str(appeal.appeal_id),
                    Agent.created_at < current,
                )
                .limit(1)
            )
            appeal_reserved_token = (
                await session.scalar(
                    select(UploadAdmissionReservation.token)
                    .where(
                        UploadAdmissionReservation.attempt_context["guidance"][
                            "appeal_id"
                        ].as_string()
                        == str(appeal.appeal_id),
                        UploadAdmissionReservation.created_at <= current,
                        UploadAdmissionReservation.expires_at > current,
                    )
                    .limit(1)
                )
                if include_reservations
                else None
            )
            if consumed is None and appeal_reserved_token is None:
                result = result.model_copy(
                    update={
                        "retry_at": None,
                        "appeal_id": appeal.appeal_id,
                        "reason": "An audited operator appeal permits this retry.",
                    }
                )
    return result


def add_attempt_record(
    session: AsyncSession,
    *,
    agent_id: UUID,
    profile: dict[str, Any],
    guidance: AttemptGuidance,
) -> None:
    session.add(
        SubmissionAttempt(
            agent_id=agent_id,
            lineage_agent_id=guidance.lineage_agent_id or agent_id,
            reference_agent_id=guidance.reference_agent_id,
            profile=profile,
            guidance=guidance.model_dump(mode="json"),
            runtime_hash=profile["runtime_hash"],
            classification=guidance.classification,
            fast_repair=guidance.fast_repair,
        )
    )

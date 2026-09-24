"""Atomic, append-only snapshots for accepted source reviews and operator rulings."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewEvent,
    ScreeningVerificationReceipt,
)
from ditto_screening_protocol import ScreenResultRequest


def _status(value: object) -> str:
    return str(getattr(value, "value", value))


async def _previous_event_id(session: AsyncSession, agent_id: UUID) -> UUID | None:
    # Callers hold the agent row lock, serializing same-agent appends.
    return await session.scalar(
        select(ScreeningReviewEvent.event_id)
        .where(ScreeningReviewEvent.agent_id == agent_id)
        .order_by(
            ScreeningReviewEvent.created_at.desc(), ScreeningReviewEvent.event_id.desc()
        )
        .limit(1)
    )


async def _receipts(session: AsyncSession, attempt_id: UUID) -> list[dict[str, str]]:
    rows = (
        await session.scalars(
            select(ScreeningVerificationReceipt)
            .where(ScreeningVerificationReceipt.attempt_id == attempt_id)
            .order_by(
                ScreeningVerificationReceipt.created_at,
                ScreeningVerificationReceipt.receipt_id,
            )
        )
    ).all()
    return [
        {
            "receipt_id": str(row.receipt_id),
            "check_code": row.check_code,
            "evidence_sha256": row.evidence_sha256,
            "image_sha256": row.image_sha256 or "",
            "profile_sha256": row.profile_sha256 or "",
            "challenge_manifest_sha256": row.challenge_manifest_sha256 or "",
        }
        for row in rows
    ]


async def append_automated_review_event(
    session: AsyncSession,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
    quarantine: ScreeningQuarantine | None,
    payload: ScreenResultRequest,
    prior_agent_status: object,
    next_agent_status: object,
    reason_code: str | None,
    reason: str | None,
) -> ScreeningReviewEvent:
    """Append once per signed attempt, in the verdict's database transaction."""
    adjudication = payload.adjudication
    event = ScreeningReviewEvent(
        event_id=uuid4(),
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        quarantine_id=quarantine.quarantine_id if quarantine else None,
        resolution_id=None,
        previous_event_id=await _previous_event_id(session, agent.agent_id),
        event_kind="automated",
        artifact_sha256=attempt.artifact_sha256 or agent.sha256,
        policy_version=attempt.policy_version,
        actor=f"screener:{attempt.screener_hotkey}",
        reviewer_model=adjudication.model if adjudication else None,
        outcome=payload.outcome.value
        if payload.outcome
        else ("pass" if payload.passed else "failed"),
        reason_code=reason_code,
        reason=reason,
        prior_agent_status=_status(prior_agent_status),
        next_agent_status=_status(next_agent_status),
        evidence={
            "manifest_digest": payload.manifest_digest,
            "finding_digest": payload.finding_digest,
            "finding": (
                payload.finding.model_dump(mode="json") if payload.finding else None
            ),
            "review_audit_digest": payload.review_audit_digest,
            "review_audit": (
                payload.review_audit.model_dump(mode="json")
                if payload.review_audit
                else None
            ),
            "review_notes_digest": payload.review_notes_digest,
            "review_notes": (
                [note.model_dump(mode="json") for note in payload.review_notes]
                if payload.review_notes
                else None
            ),
            "adjudication_digest": payload.adjudication_digest,
            "adjudication": adjudication.model_dump(mode="json")
            if adjudication
            else None,
            "review_settings_revision": attempt.review_settings_revision,
            "review_settings_checksum": attempt.review_settings_checksum,
            "verification_receipts": await _receipts(session, attempt.attempt_id),
        },
        created_at=datetime.now(UTC),
    )
    session.add(event)
    return event


async def append_manual_review_event(
    session: AsyncSession,
    *,
    agent: Agent,
    quarantine: ScreeningQuarantine,
    resolution_id: UUID,
    resolution: str,
    reason: str,
    actor: str,
    prior_agent_status: object,
    next_agent_status: object,
    created_at: datetime,
) -> ScreeningReviewEvent:
    """Snapshot an operator resolution before later rescreens change agent state."""
    attempt = await session.get(ScreeningAttempt, quarantine.attempt_id)
    if attempt is None:
        raise ValueError("quarantine has no screening attempt")
    event = ScreeningReviewEvent(
        event_id=uuid4(),
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        quarantine_id=quarantine.quarantine_id,
        resolution_id=resolution_id,
        previous_event_id=await _previous_event_id(session, agent.agent_id),
        event_kind="manual",
        artifact_sha256=attempt.artifact_sha256 or agent.sha256,
        policy_version=attempt.policy_version,
        actor=actor,
        reviewer_model=None,
        outcome=resolution,
        reason_code=quarantine.reason_code,
        reason=reason,
        prior_agent_status=_status(prior_agent_status),
        next_agent_status=_status(next_agent_status),
        evidence={
            "reason": reason,
            "manifest_digest": quarantine.manifest_digest,
            "finding_digest": quarantine.finding_digest,
            "finding": quarantine.finding,
            "review_audit_digest": quarantine.review_audit_digest,
            "review_audit": quarantine.review_audit,
            "review_notes_digest": quarantine.review_notes_digest,
            "review_notes": quarantine.review_notes,
            "verification_receipts": await _receipts(session, attempt.attempt_id),
        },
        created_at=created_at,
    )
    session.add(event)
    return event

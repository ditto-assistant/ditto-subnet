"""Ownership-gated builder for miner-private screening diagnostics."""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_screening_feedback import (
    MinerReviewNextStep,
    MinerReviewOutcome,
    MinerScreeningFailure,
    MinerScreeningFeedbackResponse,
    MinerScreeningReviewOutcome,
)
from ditto.db.models import Agent, ScreeningAttempt, ScreeningReviewEvent
from ditto_screening_protocol.models import SourceReviewAdjudication

_EFFECTIVE_OUTCOME: dict[str, MinerReviewOutcome] = {
    "reject": "rejected",
    "pass": "cleared",
    "hold": "held_for_operator_review",
}


def _review_outcome(
    event: ScreeningReviewEvent | None, *, attempt: ScreeningAttempt
) -> MinerScreeningReviewOutcome | None:
    """Reduce a verified court decision to a bounded outcome and next step.

    The outcome is what took effect for the attempt (the automated event's
    effective decision), so a shadow-posture or receipt-held decision reads as
    held rather than as the court's unapplied verdict. Provisional admission
    is not source clearance, so it has no bounded review outcome. Only a court
    decision that verifies against its signed digest produces an outcome;
    anything else is omitted. No field of the decision itself is returned.
    """
    if (
        event is None
        or not isinstance(event.evidence, dict)
        or attempt.artifact_sha256 is None
        or event.agent_id != attempt.agent_id
        or event.attempt_id != attempt.attempt_id
        or event.artifact_sha256 != attempt.artifact_sha256
        or event.policy_version != attempt.policy_version
    ):
        return None
    outcome = _EFFECTIVE_OUTCOME.get(event.effective_decision)
    raw = event.evidence.get("adjudication")
    if outcome is None or not isinstance(raw, dict):
        return None
    try:
        adjudication = SourceReviewAdjudication.model_validate(raw)
    except ValidationError:
        return None
    if (
        adjudication.canonical_digest() != event.evidence.get("adjudication_digest")
        or adjudication.policy_version != attempt.policy_version
    ):
        return None
    if (outcome == "cleared" and adjudication.decision != "clear") or (
        outcome == "rejected" and adjudication.decision != "reject"
    ):
        return None
    next_step: MinerReviewNextStep
    if outcome == "held_for_operator_review":
        next_step = "await_operator_review"
    elif outcome == "rejected":
        next_step = "resubmit_after_fix"
    else:
        next_step = "none"
    return MinerScreeningReviewOutcome(outcome=outcome, next_step=next_step)


async def load_owned_screening_feedback(
    session: AsyncSession,
    *,
    hotkey: str,
    agent_id: UUID,
) -> MinerScreeningFeedbackResponse | None:
    """Every screening attempt's private feedback for one of ``hotkey``'s agents.

    ``None`` when the agent does not exist or belongs to another hotkey; the
    caller maps both to the same 404. Source-review notes, citations, and the
    court's basis and reason stay operator-side; the submitter gets only the
    bounded ``review_outcome``.
    """
    agent = await session.scalar(
        select(Agent).where(
            Agent.agent_id == agent_id,
            Agent.miner_hotkey == hotkey,
        )
    )
    if agent is None:
        return None
    attempts = (
        await session.scalars(
            select(ScreeningAttempt)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(ScreeningAttempt.started_at.desc())
        )
    ).all()
    events = {
        event.attempt_id: event
        for event in (
            await session.scalars(
                select(ScreeningReviewEvent).where(
                    ScreeningReviewEvent.agent_id == agent_id,
                    ScreeningReviewEvent.event_kind == "automated",
                )
            )
        ).all()
    }
    return MinerScreeningFeedbackResponse(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        agent_status=str(agent.status),
        attempts=[
            MinerScreeningFailure(
                attempt_id=item.attempt_id,
                status=item.status,
                policy_version=item.policy_version,
                started_at=item.started_at,
                finished_at=item.finished_at,
                reason_code=item.reason_code,
                public_reason=item.public_reason,
                provider=item.failure_provider,
                lane=item.failure_lane,
                detail=item.private_failure_detail,
                log_tail=item.private_failure_log_tail,
                captured_at=item.failure_captured_at,
                review_outcome=_review_outcome(
                    events.get(item.attempt_id), attempt=item
                ),
            )
            for item in attempts
        ],
    )

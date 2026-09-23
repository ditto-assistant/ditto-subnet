"""Read a configured verification deadline without inventing one from policy text.

This is a storage/read foundation for #2100. It does not activate a finalizer,
authorize a retry, or establish that mandatory v13 verification passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewDeadlineActivation,
    ScreeningReviewWindow,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ReviewDeadlineBinding:
    quarantine_id: UUID
    agent_id: UUID
    attempt_id: UUID
    first_attempt_id: UUID
    artifact_sha256: str
    policy_version: int
    policy_digest: str
    activation_revision: int
    activated_at: datetime
    start_event: str
    window_started_at: datetime
    deadline_at: datetime


async def review_deadline_binding(
    session: AsyncSession, *, quarantine_id: UUID
) -> ReviewDeadlineBinding | None:
    """Return a bound deadline only for a post-activation, SHA-pinned attempt.

    No explicit per-artifact window, a legacy attempt without SHA, a changed
    artifact, or a profile mismatch returns ``None``. A future trusted writer
    must record the published start event and exact deadline on first claim;
    this read never computes one from quarantine age or a recommended default.
    """
    quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    if quarantine is None:
        return None
    attempt = await session.get(ScreeningAttempt, quarantine.attempt_id)
    agent = await session.get(Agent, quarantine.agent_id)
    if (
        attempt is None
        or agent is None
        or attempt.agent_id != quarantine.agent_id
        or attempt.policy_version != quarantine.policy_version
        or attempt.artifact_sha256 is None
        or attempt.artifact_sha256.lower() != agent.sha256.lower()
    ):
        return None
    window = await session.scalar(
        select(ScreeningReviewWindow).where(
            ScreeningReviewWindow.agent_id == quarantine.agent_id,
            ScreeningReviewWindow.policy_version == quarantine.policy_version,
        )
    )
    if window is None:
        return None
    activation = await session.get(
        ScreeningReviewDeadlineActivation, window.activation_revision
    )
    first_attempt = await session.get(ScreeningAttempt, window.first_attempt_id)
    if (
        activation is None
        or first_attempt is None
        or first_attempt.agent_id != quarantine.agent_id
        or first_attempt.policy_version != quarantine.policy_version
        or first_attempt.artifact_sha256 != window.artifact_sha256
        or _utc(first_attempt.started_at) != _utc(window.started_at)
        or _utc(attempt.started_at) < _utc(window.started_at)
        or window.artifact_sha256 != attempt.artifact_sha256
        or window.manifest_digest != quarantine.manifest_digest
        or activation.policy_version != window.policy_version
        or activation.policy_digest != window.manifest_digest
        or _utc(activation.created_at) > _utc(window.started_at)
        or _utc(activation.activate_at) > _utc(window.started_at)
        or _utc(window.deadline_at)
        != _utc(window.started_at) + timedelta(seconds=activation.window_seconds)
    ):
        return None
    # A later retry cannot be relabeled as the first claim to extend the
    # review window. Legacy/synthetic rows lack the artifact pin and cannot
    # establish this clock.
    earlier_claim = await session.scalar(
        select(ScreeningAttempt.attempt_id)
        .where(
            ScreeningAttempt.agent_id == agent.agent_id,
            ScreeningAttempt.policy_version == quarantine.policy_version,
            ScreeningAttempt.artifact_sha256 == window.artifact_sha256,
            ScreeningAttempt.attempt_id != first_attempt.attempt_id,
            ScreeningAttempt.started_at <= first_attempt.started_at,
        )
        .limit(1)
    )
    if earlier_claim is not None:
        return None
    return ReviewDeadlineBinding(
        quarantine_id=quarantine.quarantine_id,
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        first_attempt_id=first_attempt.attempt_id,
        artifact_sha256=attempt.artifact_sha256,
        policy_version=quarantine.policy_version,
        policy_digest=activation.policy_digest,
        activation_revision=activation.revision,
        activated_at=_utc(activation.activate_at),
        start_event=window.start_event,
        window_started_at=_utc(window.started_at),
        deadline_at=_utc(window.deadline_at),
    )

"""Read a configured verification deadline without inventing one from policy text.

This is a storage/read foundation for #2100. It does not activate a finalizer,
authorize a retry, or establish that mandatory v13 verification passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_review_settings import (
    ScreenerReviewSettings,
    policy_manifest_digest,
)
from ditto.db.models import (
    Agent,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewDeadlineActivation,
    ScreeningReviewWindow,
)

# SHA-256 of the published policy-v13.md carried by this build. A regression
# pins it to the source document; an operator cannot schedule an arbitrary
# normative document under the current worker binary. Recomputed after
# rebasing in the no-fault review_timed_out finalizer description (#2100/
# #1871), which changed the document's bytes.
POLICY_V13_DOCUMENT_DIGEST = (
    "0f9c46deac5b3243ad079d7aabbc792c06fb143bd6d37205818f4998f6bb5082"
)
FIRST_V13_CLAIM_EVENT: Literal["first-v13-screening-claim"] = (
    "first-v13-screening-claim"
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _database_clock(session: AsyncSession) -> datetime:
    # Production uses PostgreSQL's clock_timestamp rather than a worker clock.
    # SQLite's CURRENT_TIMESTAMP keeps local claim-path tests operational.
    clock = (
        func.clock_timestamp()
        if session.get_bind().dialect.name == "postgresql"
        else func.current_timestamp()
    )
    clock_at = await session.scalar(select(clock))
    if clock_at is None:
        raise RuntimeError("database clock unavailable")
    return _utc(clock_at)


@dataclass(frozen=True)
class ReviewDeadlineBinding:
    quarantine_id: UUID
    agent_id: UUID
    attempt_id: UUID
    first_attempt_id: UUID
    artifact_sha256: str
    policy_version: int
    policy_document_digest: str
    manifest_digest: str
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
        or activation.policy_document_digest is None
        or window.start_event != FIRST_V13_CLAIM_EVENT
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
        policy_document_digest=activation.policy_document_digest,
        manifest_digest=activation.policy_digest,
        activation_revision=activation.revision,
        activated_at=_utc(activation.activate_at),
        start_event=window.start_event,
        window_started_at=_utc(window.started_at),
        deadline_at=_utc(window.deadline_at),
    )


async def record_first_v13_claim_window(
    session: AsyncSession,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
    lease_ttl: timedelta,
) -> bool:
    """Bind a future-scheduled clock only on the first exact v13 claim.

    The caller holds the screening claim transaction/advisory lock. This
    function intentionally does nothing when no trusted activation exists,
    when the attempt is a retry, or when its immutable reviewer posture cannot
    identify the exact worker manifest. It does not finalize a review.
    """
    if (
        attempt.policy_version != 13
        or attempt.artifact_sha256 != agent.sha256
        or attempt.review_settings_revision is None
        or attempt.review_settings_checksum is None
    ):
        return False
    revision = await session.get(
        ScreenerReviewSettingsRevision, attempt.review_settings_revision
    )
    if (
        revision is None
        or revision.checksum != attempt.review_settings_checksum
        or revision.scope != attempt.review_settings_scope
    ):
        return False
    settings = ScreenerReviewSettings.model_validate(revision.settings)
    manifest_digest = policy_manifest_digest(
        settings.policy_manifest_profile, settings.policy_manifest_rotation_id
    )
    # The database time, not a worker/request timestamp, owns the start event.
    clock_at = await _database_clock(session)
    activation = await session.scalar(
        select(ScreeningReviewDeadlineActivation)
        .where(
            ScreeningReviewDeadlineActivation.policy_version == 13,
            ScreeningReviewDeadlineActivation.activate_at <= clock_at,
        )
        .order_by(ScreeningReviewDeadlineActivation.revision.desc())
        .limit(1)
    )
    if (
        activation is None
        or activation.policy_document_digest != POLICY_V13_DOCUMENT_DIGEST
        or activation.policy_digest != manifest_digest
    ):
        return False
    prior_attempt = await session.scalar(
        select(ScreeningAttempt.attempt_id)
        .where(
            ScreeningAttempt.agent_id == agent.agent_id,
            ScreeningAttempt.policy_version == 13,
            ScreeningAttempt.attempt_id != attempt.attempt_id,
        )
        .limit(1)
    )
    if prior_attempt is not None:
        return False
    existing = await session.scalar(
        select(ScreeningReviewWindow.window_id).where(
            ScreeningReviewWindow.agent_id == agent.agent_id,
            ScreeningReviewWindow.policy_version == 13,
        )
    )
    if existing is not None:
        return False
    attempt.started_at = clock_at
    attempt.deadline = clock_at + lease_ttl
    session.add(attempt)
    await session.flush()
    session.add(
        ScreeningReviewWindow(
            window_id=uuid4(),
            agent_id=agent.agent_id,
            first_attempt_id=attempt.attempt_id,
            activation_revision=activation.revision,
            artifact_sha256=agent.sha256,
            policy_version=13,
            manifest_digest=manifest_digest,
            start_event=FIRST_V13_CLAIM_EVENT,
            started_at=clock_at,
            deadline_at=clock_at + timedelta(seconds=activation.window_seconds),
        )
    )
    return True

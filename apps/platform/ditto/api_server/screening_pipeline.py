"""Shared Platform admission for enrolled screener nodes.

The Hetzner fleet performs builds, runtime checks, and source review. Platform
queues attempt-bound jobs; the signed screener result records the verdict.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.queue_policy_settings import resolve_queue_policy_settings
from ditto.db.models import (
    Agent,
    ScoredPolicyRescreenRelease,
    ScreeningAttempt,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.db.queries.screener_provider_settings import (
    resolve_screener_provider_settings,
)
from ditto.db.queries.screening import claim_screening_attempts
from ditto.screener_policy_state import effective_screening_policy_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_LEASE_TTL = timedelta(minutes=45)


async def _scored_policy_release(
    session: AsyncSession, attempt: ScreeningAttempt
) -> ScoredPolicyRescreenRelease | None:
    """Lock the one-row policy canary associated with this attempt, if any."""
    return await session.scalar(
        select(ScoredPolicyRescreenRelease)
        .where(ScoredPolicyRescreenRelease.attempt_id == attempt.attempt_id)
        .with_for_update()
    )


def fleet_lane_selected(providers: tuple[str, ...]) -> bool:
    """Use the enrolled Hetzner fleet when it is the first provider."""
    return bool(providers) and providers[0] == "hetzner"


async def admit_screening_work(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    environment: str,
    now: datetime,
    limit: int = 1,
    archive_exists: Callable[..., Awaitable[bool]] | None = None,
) -> int:
    """Open Platform-owned attempts and queue work for the enrolled fleet."""
    _, provider_settings = await resolve_screener_provider_settings(
        session, environment=environment
    )
    if not fleet_lane_selected(provider_settings.build_provider_priority):
        return 0
    queue_settings = await resolve_queue_policy_settings(session)
    claimed = await claim_screening_attempts(
        session,
        screener_hotkey=screener_hotkey,
        now=now,
        ttl=_LEASE_TTL,
        limit=limit,
        netuid=expected_netuid(),
        deferred_review_mode=queue_settings.deferred_source_review.mode,
    )
    admitted = 0
    runtime_enabled = fleet_lane_selected(provider_settings.runtime_provider_priority)
    for agent, attempt, duplicate_of in claimed:
        if duplicate_of is not None:
            attempt.status = "rejected"
            attempt.finished_at = now
            attempt.public_reason = "artifact is an exact cross-miner duplicate"
            attempt.reason_code = "exact-cross-miner-duplicate"
            agent.status = AgentStatus.REJECTED
            agent.screening_reason = attempt.public_reason
            agent.screening_reason_code = attempt.reason_code
            agent.duplicate_of = duplicate_of
            agent.screening_policy_version = effective_screening_policy_version()
            release = await _scored_policy_release(session, attempt)
            if release is not None:
                # The platform duplicate precheck is a final rejection.  Do
                # not leave the one-at-a-time policy checkpoint "running".
                release.state = "terminal"
            continue
        await _queue_submission_build(
            session,
            agent=agent,
            attempt=attempt,
            environment=environment,
            runtime_enabled=runtime_enabled,
            archive_exists=archive_exists,
        )
        if not attempt.build_only:
            await session.execute(
                pg_insert(SubmissionSourceReview)
                .values(
                    review_id=uuid4(),
                    agent_id=agent.agent_id,
                    attempt_id=attempt.attempt_id,
                    environment=environment,
                    artifact_sha256=agent.sha256.lower(),
                    status="queued",
                )
                .on_conflict_do_nothing(
                    constraint="submission_source_reviews_attempt_key"
                )
            )
        admitted += 1
    return admitted


async def _queue_submission_build(
    session: AsyncSession,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
    environment: str,
    runtime_enabled: bool,
    archive_exists: Callable[..., Awaitable[bool]] | None = None,
) -> None:
    build_id = uuid4()
    artifact_sha256 = agent.sha256.lower()
    prior_archive = await session.scalar(
        select(SubmissionImageBuild)
        .where(
            SubmissionImageBuild.agent_id == agent.agent_id,
            SubmissionImageBuild.artifact_sha256 == artifact_sha256,
            SubmissionImageBuild.environment == environment,
            SubmissionImageBuild.status.in_(("succeeded", "consumed")),
            SubmissionImageBuild.output_sha256.is_not(None),
            SubmissionImageBuild.output_size_bytes.is_not(None),
            SubmissionImageBuild.output_key.is_not(None),
            SubmissionImageBuild.output_image_id.is_not(None),
        )
        .order_by(SubmissionImageBuild.completed_at.desc())
        .limit(1)
    )
    if (
        prior_archive is not None
        and archive_exists is not None
        and not await archive_exists(key=prior_archive.output_key)
    ):
        logger.warning(
            "submission archive reuse skipped because object is missing "
            "agent_id=%s output_key=%s",
            agent.agent_id,
            prior_archive.output_key,
        )
        prior_archive = None
    if prior_archive is not None:
        now = datetime.now(UTC)
        reuse_runtime = bool(
            runtime_enabled
            and prior_archive.runtime_status == "succeeded"
            and prior_archive.runtime_image_reference is not None
        )
        await session.execute(
            pg_insert(SubmissionImageBuild)
            .values(
                build_id=build_id,
                agent_id=agent.agent_id,
                attempt_id=attempt.attempt_id,
                environment=environment,
                artifact_sha256=artifact_sha256,
                image_ref=f"ditto-screen/{agent.agent_id}-{attempt.attempt_id}:latest",
                output_key=prior_archive.output_key,
                status="succeeded",
                provider=prior_archive.provider,
                output_sha256=prior_archive.output_sha256,
                output_size_bytes=prior_archive.output_size_bytes,
                output_image_id=prior_archive.output_image_id,
                runtime_status=(
                    "succeeded"
                    if reuse_runtime
                    else ("pending" if runtime_enabled else "skipped")
                ),
                runtime_image_reference=(
                    prior_archive.runtime_image_reference if reuse_runtime else None
                ),
                runtime_error_code=(
                    None if runtime_enabled else "FLEET_RUNTIME_DISABLED_BY_POLICY"
                ),
                runtime_completed_at=now if reuse_runtime else None,
                completed_at=now,
            )
            .on_conflict_do_nothing(constraint="submission_image_builds_attempt_key")
        )
        return
    await session.execute(
        pg_insert(SubmissionImageBuild)
        .values(
            build_id=build_id,
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            environment=environment,
            artifact_sha256=artifact_sha256,
            image_ref=f"ditto-screen/{agent.agent_id}-{attempt.attempt_id}:latest",
            output_key=f"remote-builds/{build_id}/image.tar",
            status="queued",
            provider=None,
            runtime_status="pending" if runtime_enabled else "skipped",
            runtime_error_code=(
                None if runtime_enabled else "FLEET_RUNTIME_DISABLED_BY_POLICY"
            ),
        )
        .on_conflict_do_nothing(constraint="submission_image_builds_attempt_key")
    )

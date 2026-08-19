"""Platform-attested Targon screening: no GCE worker, no screener signature.

Kaniko, runtime smoke, and L1 source review are Targon jobs. Platform admits
the attempt, binds the verified Kaniko archive as the screened image, and
records the verdict. The allowlisted screener hotkey is stored as the attester
identity; Platform does not hold or use the mnemonic.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.queue_policy_settings import resolve_queue_policy_settings
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningQuarantine,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.db.queries.screener_provider_settings import (
    resolve_screener_provider_settings,
)
from ditto.db.queries.screening import claim_screening_attempts, get_screening_attempt
from ditto_screening_protocol import SourceReviewObservationPayload

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.storage.client import S3StorageClient

logger = logging.getLogger(__name__)

_LEASE_TTL = timedelta(minutes=70)
_SCREENED_IMAGE_TTL = timedelta(days=1)
_PLATFORM_COPY_PREFIX = "platform-targon-copy:"


def _targon_first(providers: tuple[str, ...]) -> bool:
    return bool(providers) and providers[0] == "targon"


def _certified_low_risk(observation: SourceReviewObservationPayload | None) -> bool:
    return bool(
        observation is not None
        and observation.ok
        and observation.risk_level == "low"
        and observation.clearance_certified
    )


async def admit_targon_screening_work(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    environment: str,
    now: datetime,
    limit: int = 1,
) -> int:
    """Open Platform-owned attempts and queue Kaniko for Targon-first lanes."""
    _, provider_settings = await resolve_screener_provider_settings(
        session, environment=environment
    )
    if not _targon_first(provider_settings.build_provider_priority):
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
    runtime_enabled = _targon_first(provider_settings.runtime_provider_priority)
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
            agent.screening_policy_version = SCREENING_POLICY_VERSION
            continue
        await _queue_kaniko(
            session,
            agent=agent,
            attempt=attempt,
            environment=environment,
            runtime_enabled=runtime_enabled,
        )
        admitted += 1
    return admitted


async def _queue_kaniko(
    session: AsyncSession,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
    environment: str,
    runtime_enabled: bool,
) -> None:
    build_id = uuid4()
    await session.execute(
        pg_insert(SubmissionImageBuild)
        .values(
            build_id=build_id,
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            environment=environment,
            artifact_sha256=agent.sha256.lower(),
            image_ref=f"ditto-screen/{agent.agent_id}-{attempt.attempt_id}:latest",
            output_key=f"remote-builds/{build_id}/image.tar",
            status="queued",
            runtime_status="pending" if runtime_enabled else "skipped",
            runtime_error_code=(
                None if runtime_enabled else "TARGON_RUNTIME_DISABLED_BY_POLICY"
            ),
        )
        .on_conflict_do_nothing(constraint="submission_image_builds_attempt_key")
    )


async def maybe_finalize_targon_screen(
    session: AsyncSession,
    *,
    storage: S3StorageClient,
    screener_hotkey: str,
    attempt_id: UUID,
    now: datetime,
) -> bool:
    """Apply a Platform-attested verdict when Targon lanes have finished."""
    attempt = await get_screening_attempt(
        session, attempt_id=attempt_id, for_update=True
    )
    if attempt is None or attempt.status != "running":
        return False
    deadline = attempt.deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if now >= deadline:
        return False
    build = await session.scalar(
        select(SubmissionImageBuild)
        .where(SubmissionImageBuild.attempt_id == attempt_id)
        .with_for_update()
    )
    if build is None or build.status not in {"succeeded", "consumed"}:
        if build is not None and build.status == "fallback_required":
            await _fail_retryable(
                session,
                attempt,
                reason="Targon submission build was unavailable",
                code="targon-build-unavailable",
                now=now,
            )
            return True
        return False
    if build.runtime_status == "fallback_required":
        await _fail_retryable(
            session,
            attempt,
            reason="Targon runtime smoke did not admit this archive",
            code="targon-runtime-unavailable",
            now=now,
        )
        return True
    if build.runtime_status != "succeeded":
        return False
    if not attempt.build_only:
        review = await session.scalar(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.attempt_id == attempt_id)
            .with_for_update()
        )
        if review is None or review.status in {"queued", "leased", "running"}:
            return False
        if review.status == "fallback_required":
            await _fail_retryable(
                session,
                attempt,
                reason="Targon source review was unavailable",
                code="targon-source-review-unavailable",
                now=now,
            )
            return True
        if review.status not in {"succeeded", "consumed"}:
            return False
        observation: SourceReviewObservationPayload | None = None
        if review.observation is not None:
            try:
                observation = SourceReviewObservationPayload.model_validate(
                    review.observation
                )
            except ValueError:
                observation = None
        if not _certified_low_risk(observation):
            await _quarantine(
                session,
                attempt=attempt,
                screener_hotkey=screener_hotkey,
                observation=observation,
                now=now,
            )
            return True
    agent = await session.get(Agent, attempt.agent_id, with_for_update=True)
    if agent is None:
        return False
    upload = await _bind_screened_image(
        session,
        storage=storage,
        agent=agent,
        attempt=attempt,
        build=build,
        screener_hotkey=screener_hotkey,
        now=now,
    )
    if upload is None:
        await _fail_retryable(
            session,
            attempt,
            reason="Platform could not bind the Targon screened image",
            code="targon-image-bind-failed",
            now=now,
        )
        return True
    agent.status = AgentStatus.EVALUATING
    agent.screening_reason = None
    agent.screening_reason_code = None
    agent.duplicate_of = None
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    agent.screened_image_sha256 = upload.sha256
    agent.screened_image_size_bytes = upload.size_bytes
    agent.screened_image_id = upload.image_id
    agent.screened_image_ref = upload.image_ref
    agent.screened_image_upload_id = upload.image_upload_id
    agent.screened_image_verified_at = upload.verified_at
    attempt.status = "passed"
    attempt.finished_at = now
    attempt.public_reason = None
    logger.info(
        "platform-attested targon pass agent_id=%s attempt_id=%s image_sha256=%s",
        agent.agent_id,
        attempt.attempt_id,
        upload.sha256,
    )
    return True


async def _fail_retryable(
    session: AsyncSession,
    attempt: ScreeningAttempt,
    *,
    reason: str,
    code: str,
    now: datetime,
) -> None:
    agent = await session.get(Agent, attempt.agent_id, with_for_update=True)
    attempt.status = "failed"
    attempt.finished_at = now
    attempt.public_reason = reason
    attempt.reason_code = code
    if agent is not None:
        agent.status = AgentStatus.SCREENING_FAILED
        agent.screening_reason = "Screening was interrupted; retry scheduled"
        agent.screening_reason_code = code


async def _quarantine(
    session: AsyncSession,
    *,
    attempt: ScreeningAttempt,
    screener_hotkey: str,
    observation: SourceReviewObservationPayload | None,
    now: datetime,
) -> None:
    agent = await session.get(Agent, attempt.agent_id, with_for_update=True)
    if agent is None:
        return
    reason_code = "agentic-source-review-tripwire"
    finding_digest = observation.finding_digest if observation is not None else None
    agent.status = AgentStatus.QUARANTINED
    agent.screening_reason = "Submission held for anti-cheat review"
    agent.screening_reason_code = reason_code
    agent.screening_policy_version = SCREENING_POLICY_VERSION
    attempt.status = "quarantined"
    attempt.finished_at = now
    attempt.public_reason = agent.screening_reason
    attempt.reason_code = reason_code
    session.add(
        ScreeningQuarantine(
            quarantine_id=uuid4(),
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            screener_hotkey=screener_hotkey,
            policy_version=SCREENING_POLICY_VERSION,
            manifest_digest=(
                observation.review_audit.canonical_digest()
                if observation is not None and observation.review_audit is not None
                else hashlib.sha256(b"ditto:platform-targon-l1:v1").hexdigest()
            ),
            finding_digest=finding_digest,
            review_audit_digest=None,
            review_audit=(
                observation.review_audit.model_dump(mode="json")
                if observation is not None and observation.review_audit is not None
                else None
            ),
            reason_code=reason_code,
            finding=(
                observation.finding.model_dump(mode="json")
                if observation is not None and observation.finding is not None
                else None
            ),
            status="active",
        )
    )


async def _bind_screened_image(
    session: AsyncSession,
    *,
    storage: S3StorageClient,
    agent: Agent,
    attempt: ScreeningAttempt,
    build: SubmissionImageBuild,
    screener_hotkey: str,
    now: datetime,
) -> ScreenedImageUpload | None:
    if (
        build.output_sha256 is None
        or build.output_size_bytes is None
        or build.output_key is None
    ):
        return None
    image_id = "sha256:" + build.output_sha256
    if build.runtime_image_reference and "@" in build.runtime_image_reference:
        digest = build.runtime_image_reference.rsplit("@", 1)[1]
        if digest.startswith("sha256:") and len(digest) == 71:
            image_id = digest
    image_ref = f"ditto-screen/{agent.agent_id}:latest"
    image_upload_id = uuid4()
    dest_key = f"{agent.agent_id}/screened-images/{image_upload_id}.tar"
    try:
        await storage.copy_object(source_key=build.output_key, dest_key=dest_key)
    except Exception:
        logger.exception(
            "platform screened-image copy failed agent_id=%s build_id=%s",
            agent.agent_id,
            build.build_id,
        )
        return None
    upload = ScreenedImageUpload(
        image_upload_id=image_upload_id,
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        screener_hotkey=screener_hotkey,
        storage_upload_id=_PLATFORM_COPY_PREFIX + str(build.build_id),
        sha256=build.output_sha256,
        size_bytes=build.output_size_bytes,
        image_id=image_id,
        image_ref=image_ref,
        status="verified",
        expires_at=now + _SCREENED_IMAGE_TTL,
        verified_at=now,
    )
    session.add(upload)
    await session.flush()
    return upload

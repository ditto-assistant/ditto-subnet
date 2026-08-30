"""Platform-attested Targon screening: no GCE worker, no screener signature.

Kaniko, runtime smoke, and L1 source review are Targon jobs. Platform admits
the attempt, binds the verified Kaniko archive as the screened image, and
records the verdict. The allowlisted screener hotkey is stored as the attester
identity; Platform does not hold or use the mnemonic.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.onchain_seed import derive_seed
from ditto.api_server.queue_policy_settings import resolve_queue_policy_settings
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    ScreenedImageUpload,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningQuarantine,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.db.queries.agents import get_agent_by_id
from ditto.db.queries.benchmark_rollout import arrival_bench_version
from ditto.db.queries.screener_provider_settings import (
    resolve_screener_provider_settings,
)
from ditto.db.queries.screening import claim_screening_attempts, get_screening_attempt
from ditto.screener_policy_state import effective_screening_policy_version
from ditto_screening_protocol import (
    SourceReviewAdjudication,
    SourceReviewObservationPayload,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_server.datapipeline import DatasetGenerator
    from ditto.api_server.storage.client import S3StorageClient
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

_LEASE_TTL = timedelta(minutes=45)
_SCREENED_IMAGE_TTL = timedelta(days=1)
_PLATFORM_COPY_PREFIX = "platform-targon-copy:"
_SUBMISSION_IMAGE_BUILD_FAILURE = re.compile(
    r"^(?:FLEET|TARGON|CLOUDRUN)_SUBMISSION_"
    r"(?:KANIKO|BUILDKIT(?:_[A-Z0-9_]{1,47})?)_FAILED$"
)
_CLOUDRUN_RUNTIME_PROVISION_CODES = frozenset(
    {
        "CLOUDRUN_PROVISION_ERROR",
        "CLOUDRUN_PROVISION_TIMEOUT",
        "CLOUDRUN_PROVIDER_ERROR",
    }
)


def remote_lane_selected(providers: tuple[str, ...]) -> bool:
    """Keep GCE-first submissions on the local screener fleet."""
    return bool(providers) and providers[0] == "targon"


def _certified_low_risk(observation: SourceReviewObservationPayload | None) -> bool:
    return bool(
        observation is not None
        and observation.ok
        and observation.risk_level == "low"
        and observation.clearance_certified
    )


def _admitted_on_coverage(
    observation: SourceReviewObservationPayload | None,
) -> bool:
    """Whether an exhausted review earned admission on its notes ledger.

    ``pass_inconclusive`` is no longer an evidence-free default: the worker
    emits it only when the review ran out of budget with ZERO recorded
    concerns and enough ``cleared`` coverage notes. Admitting it makes the
    review budget a depth knob instead of pass/fail fate; a ledger with
    concerns (or no positive coverage) arrives as ``inconclusive`` and holds
    for the operator WITH the notes attached. Legacy payloads that carry
    ``pass_inconclusive`` with no notes at all keep the old fail-closed hold.
    """
    if observation is None or observation.ok:
        return False
    if observation.failure_disposition != "pass_inconclusive":
        return False
    if observation.finding is not None or observation.finding_digest is not None:
        return False
    if any(note.kind == "concern" for note in observation.notes):
        return False
    return any(note.kind == "cleared" for note in observation.notes)


async def _effective_adjudicator_mode(
    session: AsyncSession, attempt: ScreeningAttempt
) -> str:
    """Resolve authority from the immutable settings bound at claim time.

    Closing a one-attempt canary window must not revoke L4 halfway through the
    already claimed review. Exact revision plus checksum prevents a stale or
    forged worker payload from gaining authority. Legacy unbound attempts keep
    the historical current-posture lookup.
    """
    if attempt.review_settings_revision is not None:
        row = await session.get(
            ScreenerReviewSettingsRevision, attempt.review_settings_revision
        )
        if row is None or row.checksum != attempt.review_settings_checksum:
            return "off"
    else:
        row = await session.scalar(
            select(ScreenerReviewSettingsRevision)
            .where(ScreenerReviewSettingsRevision.scope == "*")
            .order_by(ScreenerReviewSettingsRevision.revision.desc())
            .limit(1)
        )
    if row is None:
        return "off"
    try:
        return ScreenerReviewSettings.model_validate(row.settings).adjudicator_mode
    except ValueError:
        return "off"


def _adjudication_basis(adjudication: SourceReviewAdjudication) -> str:
    """The published clause, invariant, or refusal code behind a decision."""
    return (
        adjudication.escalation_code
        or adjudication.reject_invariant
        or adjudication.clear_clause
        or "none"
    )


def _actionable_adjudication(
    observation: SourceReviewObservationPayload | None,
) -> SourceReviewAdjudication | None:
    """The decision to execute, or ``None`` to keep holding.

    ``escalate`` is the adjudicator's own refusal -- it could not verify its
    citations -- and is deliberately indistinguishable from having no
    adjudication at all here: the submission holds for an operator either way.
    """
    if observation is None or observation.adjudication is None:
        return None
    if observation.adjudication.decision not in {"clear", "reject"}:
        return None
    return observation.adjudication


def _review_failed_retryable(
    observation: SourceReviewObservationPayload | None,
) -> bool:
    """Whether the reviewer reported a pre-verdict infrastructure failure.

    The worker marks transport and model-output failures (for example
    ``source-review-model-response-invalid``) as ``retryable_infra`` expecting
    the attempt to burn and rescreen. Quarantining them instead held miners
    under an anti-cheat label for what the reviewer itself called an outage.
    Only the explicit self-report with no finding qualifies: there is no
    evidence to weigh, so retrying cannot release anything the court derived.
    Every other non-certified observation — a finding, an elevated risk, an
    inconclusive budget outcome, or an unparseable payload — stays fail-closed
    in quarantine.
    """
    return bool(
        observation is not None
        and not observation.ok
        and observation.finding is None
        and observation.finding_digest is None
        and observation.failure_disposition == "retryable_infra"
    )


async def admit_targon_screening_work(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    environment: str,
    now: datetime,
    limit: int = 1,
    archive_exists: Callable[..., Awaitable[bool]] | None = None,
) -> int:
    """Open Platform-owned attempts and queue Kaniko for Targon-first lanes."""
    _, provider_settings = await resolve_screener_provider_settings(
        session, environment=environment
    )
    if not remote_lane_selected(provider_settings.build_provider_priority):
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
    runtime_enabled = remote_lane_selected(provider_settings.runtime_provider_priority)
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
            continue
        await _queue_kaniko(
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


async def _queue_kaniko(
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
                    None if runtime_enabled else "TARGON_RUNTIME_DISABLED_BY_POLICY"
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
            if _SUBMISSION_IMAGE_BUILD_FAILURE.fullmatch(build.error_code or ""):
                await _reject_build(
                    session,
                    attempt,
                    reason="artifact Docker image did not build",
                    code="docker-build",
                    now=now,
                )
            else:
                gcp_failed = build.provider == "gcp"
                await _fail_retryable(
                    session,
                    attempt,
                    reason=(
                        "Cloud Run submission build was unavailable"
                        if gcp_failed
                        else "Targon submission build was unavailable"
                    ),
                    code=(
                        "cloudrun-build-unavailable"
                        if gcp_failed
                        else "targon-build-unavailable"
                    ),
                    now=now,
                )
            return True
        return False
    if build.runtime_status == "fallback_required":
        cloudrun_runtime = (
            build.runtime_error_code or ""
        ) in _CLOUDRUN_RUNTIME_PROVISION_CODES
        await _fail_retryable(
            session,
            attempt,
            reason=(
                "Cloud Run runtime smoke was unavailable"
                if cloudrun_runtime
                else "Targon runtime smoke did not admit this archive"
            ),
            code=(
                "cloudrun-runtime-unavailable"
                if cloudrun_runtime
                else "targon-runtime-unavailable"
            ),
            now=now,
        )
        return True
    if build.runtime_status != "succeeded":
        return False
    coverage_admitted = False
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
        coverage_admitted = _admitted_on_coverage(observation)
        if not _certified_low_risk(observation) and not coverage_admitted:
            adjudication = _actionable_adjudication(observation)
            if adjudication is not None and (
                await _effective_adjudicator_mode(session, attempt) == "enforce"
            ):
                if adjudication.decision == "reject":
                    await _reject_build(
                        session,
                        attempt,
                        reason=adjudication.reason,
                        code="adjudicated-source-review-reject",
                        now=now,
                    )
                    return True
                coverage_admitted = True
            if (
                not coverage_admitted
                and (observation is None or observation.adjudication is None)
                and _review_failed_retryable(observation)
            ):
                await _fail_retryable(
                    session,
                    attempt,
                    reason="Agentic source review failed before reaching a verdict",
                    code="source-review-retryable-infra",
                    now=now,
                )
                return True
            if not coverage_admitted:
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
    agent.screening_policy_version = effective_screening_policy_version()
    agent.screened_image_sha256 = upload.sha256
    agent.screened_image_size_bytes = upload.size_bytes
    agent.screened_image_id = upload.image_id
    agent.screened_image_ref = upload.image_ref
    agent.screened_image_upload_id = upload.image_upload_id
    agent.screened_image_verified_at = upload.verified_at
    attempt.status = "passed"
    attempt.finished_at = now
    attempt.public_reason = (
        "Bounded source review exhausted with clean coverage notes; admitted"
        if coverage_admitted
        else None
    )
    logger.info(
        "platform-attested targon pass agent_id=%s attempt_id=%s image_sha256=%s",
        agent.agent_id,
        attempt.attempt_id,
        upload.sha256,
    )
    return True


async def ensure_arrival_dataset(
    session: AsyncSession,
    *,
    agent_id: UUID,
    generator: DatasetGenerator,
    chain: ChainClient | None,
) -> None:
    """Pin the arrival-era dataset after a Targon-attested pass.

    ``maybe_finalize_targon_screen`` used to promote EVALUATING with a verified
    image and no BenchmarkDataset row. Claim then treated that as missing
    dataset work and rebuilt Kaniko forever.
    """
    if generator.run_size is None:
        return
    async with session.begin():
        agent = await get_agent_by_id(session, agent_id=agent_id)
        if agent is None:
            return
        bench_version = await arrival_bench_version(session, agent=agent)
        existing = await session.get(BenchmarkDataset, (agent_id, bench_version))
        if existing is not None:
            return
        seed = agent.dataset_seed
        block_number = agent.dataset_seed_block
        block_hash = agent.dataset_seed_block_hash
    if seed is None:
        seed, block_number, block_hash = await _dataset_seed(chain, agent_id)
    sha256 = await generator.generate(seed, bench_version=bench_version)
    async with session.begin():
        existing = await session.get(BenchmarkDataset, (agent_id, bench_version))
        if existing is not None:
            return
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        if agent is None:
            return
        session.add(
            BenchmarkDataset(
                agent_id=agent_id,
                bench_version=bench_version,
                seed=seed,
                sha256=sha256,
                run_size=generator.run_size,
                seed_block=block_number,
                seed_block_hash=block_hash,
            )
        )
        if agent.dataset_seed is None:
            agent.dataset_seed = seed
            agent.dataset_sha256 = sha256
            agent.dataset_run_size = generator.run_size
            agent.dataset_seed_block = block_number
            agent.dataset_seed_block_hash = block_hash


async def finalize_targon_screen_and_pin_dataset(
    session: AsyncSession,
    *,
    storage: S3StorageClient,
    screener_hotkey: str,
    attempt_id: UUID,
    now: datetime,
    generator: DatasetGenerator | None,
    chain: ChainClient | None,
) -> bool:
    """Finalize a ready Targon attempt, then pin its arrival dataset."""
    async with session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=storage,
            screener_hotkey=screener_hotkey,
            attempt_id=attempt_id,
            now=now,
        )
        attempt = await get_screening_attempt(session, attempt_id=attempt_id)
        agent_id = (
            attempt.agent_id
            if attempt is not None and attempt.status == "passed"
            else None
        )
    if agent_id is None or generator is None:
        return finalized
    await ensure_arrival_dataset(
        session,
        agent_id=agent_id,
        generator=generator,
        chain=chain,
    )
    return finalized


async def _dataset_seed(
    chain: ChainClient | None, agent_id: UUID
) -> tuple[int, int | None, str | None]:
    if chain is None:
        return secrets.randbits(63), None, None
    try:
        block = await chain.get_latest_block()
    except Exception:
        logger.warning(
            "on-chain seed derivation unavailable; using local seed agent_id=%s",
            agent_id,
        )
        return secrets.randbits(63), None, None
    return derive_seed(block.hash, agent_id), block.number, block.hash


async def _reject_build(
    session: AsyncSession,
    attempt: ScreeningAttempt,
    *,
    reason: str,
    code: str,
    now: datetime,
) -> None:
    agent = await session.get(Agent, attempt.agent_id, with_for_update=True)
    attempt.status = "rejected"
    attempt.finished_at = now
    attempt.public_reason = reason
    attempt.reason_code = code
    if agent is not None:
        agent.status = AgentStatus.REJECTED
        agent.screening_reason = reason
        agent.screening_reason_code = code
        agent.screening_policy_version = effective_screening_policy_version()


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
        agent.screening_reason = (
            "Screening failed and is parked; an operator may retry it in Backroom"
        )
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
    finding_digest = observation.finding_digest if observation is not None else None
    inconclusive_budget = bool(
        observation is not None
        and not observation.ok
        and observation.finding is None
        and finding_digest is None
        and observation.failure_disposition in ("inconclusive", "pass_inconclusive")
    )
    if inconclusive_budget:
        reason_code = "source-review-inconclusive"
        public_reason = "Bounded source review was inconclusive; held for review"
    else:
        reason_code = "agentic-source-review-tripwire"
        public_reason = "Submission held for anti-cheat review"
    review_audit = observation.review_audit if observation is not None else None
    # The reviewer's in-progress notes ledger is the operator's material for a
    # budget-terminated review: map it onto the bounded public-safe evidence
    # trail so Backroom shows WHAT the court determined before it ran out.
    note_evidence = [
        {
            "module": "review-note",
            "code": f"{note.kind}:{note.category}",
            "summary": (
                (f"{note.path}:{note.line} — " if note.path and note.line else "")
                + note.summary
            )[:300],
        }
        for note in (observation.notes if observation is not None else [])
    ][:48]
    # A hold that HAS an adjudication is either running in shadow or was
    # refused by the adjudicator's own citation checks. Either way the operator
    # who now has to decide it should see what the court concluded and why it
    # did not carry, rather than re-deriving it.
    adjudication = observation.adjudication if observation is not None else None
    if adjudication is not None:
        note_evidence.append(
            {
                "module": "adjudication",
                "code": (
                    f"{adjudication.decision}:{_adjudication_basis(adjudication)}"
                ),
                "summary": adjudication.reason[:300],
            }
        )
    agent.status = AgentStatus.QUARANTINED
    agent.screening_reason = public_reason
    agent.screening_reason_code = reason_code
    agent.screening_policy_version = effective_screening_policy_version()
    attempt.status = "quarantined"
    attempt.finished_at = now
    attempt.public_reason = public_reason
    attempt.reason_code = reason_code
    session.add(
        ScreeningQuarantine(
            quarantine_id=uuid4(),
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            screener_hotkey=screener_hotkey,
            policy_version=effective_screening_policy_version(),
            manifest_digest=(
                review_audit.canonical_digest()
                if review_audit is not None
                else hashlib.sha256(b"ditto:platform-targon-l1:v1").hexdigest()
            ),
            finding_digest=finding_digest,
            review_audit_digest=(
                review_audit.canonical_digest() if review_audit is not None else None
            ),
            review_audit=(
                review_audit.model_dump(mode="json")
                if review_audit is not None
                else None
            ),
            reason_code=reason_code,
            evidence=note_evidence or None,
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
    image_id = build.output_image_id
    if image_id is None:
        logger.error(
            "Kaniko tar config digest missing agent_id=%s build_id=%s",
            agent.agent_id,
            build.build_id,
        )
        return None
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


_REPAIR_LIMIT = 8


async def repair_kaniko_screened_image_identities(
    session_maker: async_sessionmaker,
    *,
    limit: int = _REPAIR_LIMIT,
) -> int:
    """Re-pin Targon/Cloud Run images to the builder-posted tar config digest.

    The builder hashes ``{configDigest}.json`` from the Kaniko docker-save
    and stores it as ``output_image_id``. Artifact Registry inspect is a
    different identity. Rows without a stored digest are skipped — rebuild
    them after this pin is live. Never downloads a tar and never inspects
    the registry.
    """
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(Agent, ScreenedImageUpload, SubmissionImageBuild)
                .join(
                    ScreenedImageUpload,
                    ScreenedImageUpload.image_upload_id
                    == Agent.screened_image_upload_id,
                )
                .join(
                    SubmissionImageBuild,
                    SubmissionImageBuild.attempt_id == ScreenedImageUpload.attempt_id,
                )
                .where(
                    Agent.status == AgentStatus.EVALUATING,
                    Agent.screened_image_id.is_not(None),
                    SubmissionImageBuild.status.in_(("succeeded", "consumed")),
                    SubmissionImageBuild.provider.in_(("targon", "gcp")),
                    SubmissionImageBuild.output_image_id.is_not(None),
                    Agent.screened_image_id != SubmissionImageBuild.output_image_id,
                )
                .limit(limit)
            )
        ).all()
    repaired = 0
    for agent_row, upload_row, build in rows:
        digest = build.output_image_id
        current_id = agent_row.screened_image_id
        if digest is None or current_id is None or digest == current_id:
            continue
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, agent_row.agent_id, with_for_update=True)
            upload = await session.get(
                ScreenedImageUpload,
                upload_row.image_upload_id,
                with_for_update=True,
            )
            if (
                agent is None
                or upload is None
                or agent.screened_image_id != current_id
                or agent.status != AgentStatus.EVALUATING
            ):
                continue
            agent.screened_image_id = digest
            upload.image_id = digest
        logger.info(
            "re-pinned Kaniko screened image agent_id=%s from %s to %s",
            agent_row.agent_id,
            current_id,
            digest,
        )
        repaired += 1
    return repaired

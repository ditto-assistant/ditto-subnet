"""Read an existing authenticated job observation without rerunning review."""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.source_review_evidence import StoredSourceReviewEvidence
from ditto.db.models import Agent, ScreeningAttempt, SubmissionSourceReview
from ditto_screening_protocol import SourceReviewObservationPayload


async def stored_source_review_evidence(
    session: AsyncSession, *, agent: Agent, attempt: ScreeningAttempt
) -> StoredSourceReviewEvidence | None:
    row = await session.scalar(
        select(SubmissionSourceReview).where(
            SubmissionSourceReview.agent_id == agent.agent_id,
            SubmissionSourceReview.attempt_id == attempt.attempt_id,
        )
    )
    if row is None:
        return None
    result = StoredSourceReviewEvidence(
        review_id=row.review_id,
        status=row.status,
        artifact_sha256=row.artifact_sha256,
        binding_matches=(
            row.artifact_sha256 == agent.sha256
            and attempt.artifact_sha256 == agent.sha256
        ),
        observation_status="not_recorded" if row.observation is None else "invalid",
        completed_at=row.completed_at,
        consumed_at=row.consumed_at,
    )
    if row.observation is None:
        return result
    try:
        observation = SourceReviewObservationPayload.model_validate(row.observation)
        if observation.finding is not None:
            observation.finding.require_policy_v10_invariants()
    except (ValidationError, ValueError):
        return result
    finding = observation.finding
    if (
        not result.binding_matches
        or row.status not in {"succeeded", "consumed"}
        or row.completed_at is None
    ):
        return result
    result.observation_ok = observation.ok
    if finding is None:
        result.observation_status = "finding_not_recorded"
        result.clearance_certified = observation.clearance_certified
        return result
    if (
        finding.artifact_sha256 != row.artifact_sha256
        or not finding.prompt_revision.endswith(f"-policy-v{attempt.policy_version}")
        or finding.invariant_assessment is None
        or finding.invariant_assessment.schema_version
        != (2 if attempt.policy_version >= 13 else 1)
    ):
        return result
    result.observation_status = "recorded_unverified"
    result.clearance_certified = observation.clearance_certified
    result.finding_digest = observation.finding_digest
    result.finding = finding
    return result

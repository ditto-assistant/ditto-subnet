"""Append one policy-v13 screening decision record.

Every terminal review decision -- an operator ``clear`` / ``reject`` through
``resolve_ath_review``, a Platform automatic clear (owner-link attestation,
deferred source review completing without a finding) or the deadline
finalizer's no-fault ``review_timed_out`` -- is recorded here so
``GET /admin/screening-decisions/{agent_id}`` shows one complete history.
Lives under ``db.queries`` because both API endpoints and lower-level query
modules (``attestation``) write records.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, ScreeningDecisionRecord
from ditto.db.queries.benchmark_rollout import arrival_bench_version
from ditto_screening_protocol import FailureDomain, ScreeningDecisionOutcome

# Reviewer recorded on decisions the Platform takes without an operator.
PLATFORM_AUTOMATIC_CLEAR_REVIEWER = "platform:automatic-clear"


async def record_screening_decision(
    session: AsyncSession,
    *,
    agent: Agent,
    outcome: ScreeningDecisionOutcome,
    reason_codes: list[str],
    violation_proven: bool,
    failure_domain: FailureDomain,
    retry_count: int,
    independent_workers: int,
    policy_version: int,
    public_reason: str,
    reviewer: str,
    decided_at: datetime,
    evidence_references: list[str],
    completed_checks: list[str],
    failed_checks: list[str],
    limitations: list[str],
    attempt_id: UUID | None = None,
    quarantine_id: UUID | None = None,
    review_id: UUID | None = None,
    review_scope: str | None = None,
    evidence_type: str | None = None,
    opaque_components: list[str] | None = None,
    policy_digest: str | None = None,
    verification_profile_digest: str | None = None,
    precedent_weight: bool = False,
    retry_grant_id: UUID | None = None,
    operator_override: dict[str, object] | None = None,
) -> ScreeningDecisionRecord:
    """Append one policy-v13 decision record for ``agent``.

    The previous record for the same agent (if any) is linked through
    ``supersedes_decision`` so appeals and re-reviews keep their history.
    Runs inside the caller's transaction and never swallows a database error:
    an aborted asyncpg transaction cannot flush anyway, so the caller's
    rollback (and the finalizer's next tick) is the recovery path.
    """
    previous_id = await session.scalar(
        select(ScreeningDecisionRecord.decision_id)
        .where(ScreeningDecisionRecord.agent_id == agent.agent_id)
        .order_by(
            ScreeningDecisionRecord.decided_at.desc(),
            ScreeningDecisionRecord.decision_id.desc(),
        )
        .limit(1)
    )
    benchmark_version = await arrival_bench_version(session, agent=agent)
    record = ScreeningDecisionRecord(
        decision_id=uuid4(),
        agent_id=agent.agent_id,
        attempt_id=attempt_id,
        quarantine_id=quarantine_id,
        review_id=review_id,
        outcome=outcome,
        reason_codes=list(reason_codes),
        violation_proven=violation_proven,
        failure_domain=failure_domain,
        retry_count=retry_count,
        independent_workers=independent_workers,
        policy_version=policy_version,
        identities={
            "submission_uuid": str(agent.agent_id),
            "artifact_sha256": agent.sha256,
            "image_digest": agent.screened_image_id,
            "build_configuration": None,
            "served_entrypoint": None,
            "permitted_runtime_configuration": agent.dataset_run_size,
            "benchmark_version": benchmark_version,
            "applied_policy_version": policy_version,
            "policy_digest": policy_digest,
            "verification_profile_digest": verification_profile_digest,
        },
        review_scope=review_scope,
        completed_checks=list(completed_checks),
        failed_checks=list(failed_checks),
        opaque_components=list(opaque_components or []),
        evidence_references=list(evidence_references),
        evidence_type=evidence_type,
        limitations=list(limitations),
        public_reason=public_reason,
        reviewer=reviewer,
        decided_at=decided_at,
        supersedes_decision=previous_id,
        operator_override=operator_override,
        precedent_weight=precedent_weight,
        retry_grant_id=retry_grant_id,
    )
    session.add(record)
    await session.flush()
    return record


async def record_automatic_clear(
    session: AsyncSession,
    *,
    agent: Agent,
    review_id: UUID,
    review_scope: str,
    public_reason: str,
    reviewer: str,
    evidence_references: list[str],
    evidence_type: str,
    decided_at: datetime,
    attempt_id: UUID | None = None,
) -> ScreeningDecisionRecord:
    """Record a Platform-automatic ATH clear (no operator, no precedent).

    An automatic clear proves nothing about the artifact -- it says the hold's
    premise (a copy signal, a deferred review) resolved without a finding --
    so it carries ``violation_proven: false``, ``failure_domain: none`` and
    ``precedent_weight: false``, and cites the platform record that released
    it rather than a file:line reference.
    """
    return await record_screening_decision(
        session,
        agent=agent,
        outcome="clear",
        reason_codes=[],
        violation_proven=False,
        failure_domain="none",
        retry_count=0,
        independent_workers=0,
        policy_version=max(1, int(agent.screening_policy_version or 0)),
        public_reason=public_reason,
        reviewer=reviewer,
        decided_at=decided_at,
        evidence_references=list(evidence_references),
        completed_checks=[review_scope],
        failed_checks=[],
        limitations=["automatic platform clear; no operator source review"],
        attempt_id=attempt_id,
        review_id=review_id,
        review_scope=review_scope,
        evidence_type=evidence_type,
        precedent_weight=False,
    )

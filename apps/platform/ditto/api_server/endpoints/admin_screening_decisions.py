"""Read the policy-v13 screening decision records Backroom cites.

``GET /admin/screening-decisions`` pages every recorded decision (filter by
outcome; ``review_timed_out`` counts are the activation monitor), and
``GET /admin/screening-decisions/{agent_id}`` returns one agent's history
newest first. Both fold in the published retry/deadline policy and capacity
thresholds so a timeout is always read next to the bar it was held to. Read
only: decisions are written by ``resolve_ath_review`` and the finalizer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screening_decision import (
    AdminScreeningDecisionList,
    AdminScreeningDecisionRecordResponse,
    AdminScreeningVerificationState,
    ScreeningDecisionIdentities,
    ScreeningDecisionOutcome,
    ScreeningDecisionRecordView,
    ScreeningFailureDomain,
    review_capacity_thresholds_view,
    review_timeout_policy_view,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.review_timeout_finalizer import configured_finalizer_mode
from ditto.db.models import (
    Agent,
    ScoredPolicyRescreenRelease,
    ScreeningAttempt,
    ScreeningDecisionRecord,
    ScreeningQuarantine,
)
from ditto_screening_protocol import (
    NON_DECISIVE_REASON_CODES,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    failure_domain_for_reason_code,
)

router = APIRouter(prefix="/admin/screening-decisions", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _view(row: ScreeningDecisionRecord) -> ScreeningDecisionRecordView:
    identities = row.identities if isinstance(row.identities, dict) else {}
    return ScreeningDecisionRecordView(
        decision_id=row.decision_id,
        agent_id=row.agent_id,
        attempt_id=row.attempt_id,
        quarantine_id=row.quarantine_id,
        review_id=row.review_id,
        outcome=cast(ScreeningDecisionOutcome, row.outcome),
        reason_codes=_strings(row.reason_codes),
        violation_proven=row.violation_proven,
        failure_domain=cast(ScreeningFailureDomain, row.failure_domain),
        retry_count=row.retry_count,
        independent_workers=row.independent_workers,
        policy_version=row.policy_version,
        identities=ScreeningDecisionIdentities.model_validate(
            {
                "submission_uuid": identities.get("submission_uuid", row.agent_id),
                "artifact_sha256": identities.get("artifact_sha256", ""),
                "image_digest": identities.get("image_digest"),
                "build_configuration": identities.get("build_configuration"),
                "served_entrypoint": identities.get("served_entrypoint"),
                "permitted_runtime_configuration": identities.get(
                    "permitted_runtime_configuration"
                ),
                "benchmark_version": identities.get("benchmark_version"),
                "applied_policy_version": identities.get(
                    "applied_policy_version", row.policy_version
                ),
                "policy_digest": identities.get("policy_digest"),
                "verification_profile_digest": identities.get(
                    "verification_profile_digest"
                ),
            }
        ),
        review_scope=row.review_scope,
        completed_checks=_strings(row.completed_checks),
        failed_checks=_strings(row.failed_checks),
        opaque_components=_strings(row.opaque_components),
        evidence_references=_strings(row.evidence_references),
        evidence_type=row.evidence_type,
        limitations=_strings(row.limitations),
        public_reason=row.public_reason,
        reviewer=row.reviewer,
        decided_at=row.decided_at,
        supersedes_decision=row.supersedes_decision,
        operator_override=(
            row.operator_override if isinstance(row.operator_override, dict) else None
        ),
        precedent_weight=row.precedent_weight,
        retry_grant_id=row.retry_grant_id,
        is_verification_failure=(
            row.outcome == REVIEW_TIMED_OUT_OUTCOME
            or (row.outcome == "reject" and not row.violation_proven)
        ),
        no_fault=row.outcome == REVIEW_TIMED_OUT_OUTCOME,
    )


@router.get("", response_model=AdminScreeningDecisionList)
async def list_screening_decisions(
    _admin: AdminDep,
    session: SessionDep,
    outcome: Annotated[
        Literal["clear", "reject", "review_timed_out"] | None, Query()
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminScreeningDecisionList:
    """Page decision records newest first, with subnet-wide outcome counts."""
    stmt = select(ScreeningDecisionRecord).order_by(
        ScreeningDecisionRecord.decided_at.desc(),
        ScreeningDecisionRecord.decision_id.desc(),
    )
    count_stmt = select(func.count()).select_from(ScreeningDecisionRecord)
    if outcome is not None:
        stmt = stmt.where(ScreeningDecisionRecord.outcome == outcome)
        count_stmt = count_stmt.where(ScreeningDecisionRecord.outcome == outcome)
    rows = list(await session.scalars(stmt.offset(offset).limit(limit)))
    count = await session.scalar(count_stmt)
    outcome_rows = (
        await session.execute(
            select(ScreeningDecisionRecord.outcome, func.count()).group_by(
                ScreeningDecisionRecord.outcome
            )
        )
    ).all()
    outcome_counts = dict.fromkeys(("clear", "reject", REVIEW_TIMED_OUT_OUTCOME), 0)
    for key, value in outcome_rows:
        outcome_counts[str(key)] = int(value or 0)
    return AdminScreeningDecisionList(
        items=[_view(row) for row in rows],
        count=int(count or 0),
        limit=limit,
        offset=offset,
        outcome=outcome,
        outcome_counts=outcome_counts,
        review_timeout_policy=review_timeout_policy_view(),
        review_capacity_thresholds=review_capacity_thresholds_view(),
    )


@router.get("/{agent_id}", response_model=AdminScreeningDecisionRecordResponse)
async def get_screening_decision_record(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminScreeningDecisionRecordResponse:
    """Every decision recorded for one agent, newest first.

    An agent with no record is a 200 with an empty history, not a 404: the
    operator's question is "what was decided", and "nothing yet" answers it.
    """
    agent = await session.get(Agent, agent_id)
    rows = list(
        await session.scalars(
            select(ScreeningDecisionRecord)
            .where(ScreeningDecisionRecord.agent_id == agent_id)
            .order_by(
                ScreeningDecisionRecord.decided_at.desc(),
                ScreeningDecisionRecord.decision_id.desc(),
            )
        )
    )
    decisions = [_view(row) for row in rows]
    return AdminScreeningDecisionRecordResponse(
        agent_id=agent_id,
        agent_status=agent.status.value if agent is not None else None,
        latest=decisions[0] if decisions else None,
        decisions=decisions,
        review_timeout_policy=review_timeout_policy_view(),
        review_capacity_thresholds=review_capacity_thresholds_view(),
    )


@router.get(
    "/{agent_id}/verification-state", response_model=AdminScreeningVerificationState
)
async def get_screening_verification_state(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminScreeningVerificationState:
    """Report only finalizer facts persisted for this exact submission.

    The attempt lease deadline is distinct from the finalizer's artifact
    deadline. In off/shadow mode no enforceable deadline is returned. The
    policy's recommended window becomes effective only when the finalizer is
    configured to enforce it.
    """
    agent = await session.get(Agent, agent_id)
    quarantine = await session.scalar(
        select(ScreeningQuarantine)
        .where(
            ScreeningQuarantine.agent_id == agent_id,
            ScreeningQuarantine.status == "active",
        )
        .order_by(ScreeningQuarantine.created_at.desc())
        .limit(1)
    )
    decision_row = await session.scalar(
        select(ScreeningDecisionRecord)
        .where(ScreeningDecisionRecord.agent_id == agent_id)
        .order_by(ScreeningDecisionRecord.decided_at.desc())
        .limit(1)
    )
    latest_decision = _view(decision_row) if decision_row is not None else None
    decision_matches_artifact = (
        decision_row.identities.get("artifact_sha256") == agent.sha256
        if decision_row is not None
        and agent is not None
        and isinstance(decision_row.identities, dict)
        else None
    )
    attempt = (
        await session.get(ScreeningAttempt, quarantine.attempt_id)
        if quarantine is not None
        else None
    )
    release = (
        await session.scalar(
            select(ScoredPolicyRescreenRelease).where(
                ScoredPolicyRescreenRelease.attempt_id == quarantine.attempt_id
            )
        )
        if quarantine is not None
        else None
    )
    policy = PUBLISHED_REVIEW_TIMEOUT_POLICY
    evidence_rows = (
        await session.execute(
            select(
                func.count(ScreeningAttempt.attempt_id),
                func.count(func.distinct(ScreeningAttempt.screener_hotkey)),
            ).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.policy_version >= policy.applies_from_policy_version,
            )
        )
    ).one()
    attempts_recorded = int(evidence_rows[0] or 0)
    independent_workers = int(evidence_rows[1] or 0)
    retries_used = policy.automatic_retries_used(attempts_recorded)
    failure_domain = (
        failure_domain_for_reason_code(
            quarantine.reason_code,
            failure_provider=attempt.failure_provider if attempt is not None else None,
        )
        if quarantine is not None
        and quarantine.reason_code in NON_DECISIVE_REASON_CODES
        else None
    )
    mode = configured_finalizer_mode()
    state = "not_configured"
    reason = "no enforceable finalizer deadline is configured"
    started_at = None
    deadline = None
    provenance = None
    if quarantine is not None:
        if quarantine.policy_version < policy.applies_from_policy_version:
            reason = "legacy policy is outside the v13 finalizer"
        elif (
            quarantine.reason_code not in NON_DECISIVE_REASON_CODES
            or quarantine.finding is not None
            or quarantine.finding_digest is not None
        ):
            reason = (
                "finding-backed or other operator hold is outside the timeout finalizer"
            )
        elif mode != "enforce":
            reason = f"finalizer mode is {mode}; no enforceable deadline"
        elif (
            agent is None
            or (release is None and agent.status != AgentStatus.QUARANTINED)
            or (release is not None and release.state != "paused")
        ):
            reason = (
                "agent or scored-rescreen release is outside the "
                "finalizer's eligible state"
            )
        else:
            started_at = quarantine.created_at
            deadline = started_at + policy.max_verification_window
            provenance = (
                "shipped_finalizer_default; no artifact activation revision is stored"
            )
            state = "ready" if datetime.now(UTC) >= deadline else "pending"
            reason = "active non-decisive v13 quarantine eligible for no-fault timeout"
    elif latest_decision is not None and decision_matches_artifact:
        state = "finalized"
        reason = "terminal decision recorded for this artifact"
    elif latest_decision is not None:
        reason = "latest decision belongs to a different artifact digest"
    elif agent is None:
        reason = "agent not found"
    else:
        reason = "no active finalizer-eligible quarantine or terminal decision"
    identities = (
        latest_decision.identities
        if latest_decision and decision_matches_artifact
        else None
    )
    return AdminScreeningVerificationState(
        agent_id=agent_id,
        agent_status=agent.status.value if agent is not None else None,
        artifact_sha256=agent.sha256 if agent is not None else None,
        decision_matches_artifact=decision_matches_artifact,
        policy_version=quarantine.policy_version
        if quarantine is not None
        else (agent.screening_policy_version if agent is not None else None),
        policy_digest=quarantine.manifest_digest
        if quarantine is not None
        else (identities.policy_digest if identities is not None else None),
        quarantine_id=quarantine.quarantine_id if quarantine is not None else None,
        attempt_id=quarantine.attempt_id if quarantine is not None else None,
        reason_code=quarantine.reason_code if quarantine is not None else None,
        finalizer_mode=mode,
        finalizer_state=state,
        finalizer_reason=reason,
        verification_started_at=started_at,
        verification_deadline=deadline,
        deadline_provenance=provenance,
        attempt_deadline=attempt.deadline if attempt is not None else None,
        failure_domain=failure_domain,
        automatic_retry_budget=policy.automatic_retry_budget(failure_domain)
        if failure_domain
        else None,
        retries_used=retries_used,
        attempts_recorded=attempts_recorded,
        independent_workers=independent_workers,
        independent_worker_required=policy.independent_worker_required(failure_domain)
        if failure_domain
        else None,
        latest_decision=latest_decision,
        mandatory_checks_state="terminal_record_only"
        if identities is not None
        else "not_recorded",
        image_digest=identities.image_digest if identities is not None else None,
        build_configuration=identities.build_configuration
        if identities is not None
        else None,
        permitted_runtime_configuration=(
            identities.permitted_runtime_configuration
            if identities is not None
            else None
        ),
    )

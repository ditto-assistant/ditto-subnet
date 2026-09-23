"""Read-only v13 verification-deadline and finalizer-state visibility (#2100).

``GET /admin/screening-verification-deadline/{agent_id}`` answers, for one
exact agent, whether a live artifact-level v13 deadline governs its current
hold (if any), whether that deadline has passed, and what state the deadline
finalizer (``ditto.api_server.review_timeout_finalizer``) would treat it as --
without mutating anything and without inventing a deadline the platform has
not actually computed from a stored quarantine. See
``ditto.api_models.screening_verification_deadline`` for the full field
contract and the ``finalizer_state`` design.

This is a sibling of ``admin_screening_decisions``, not a replacement: that
router answers "what was decided"; this one answers "is this exact held row
inside its verification window, and would the finalizer act on it" for a
submission that has not (yet) reached a decision. When a decision already
exists for the current hold, this route defers to it completely --
``screening_decision_record_view`` renders it verbatim, nothing here is
recomputed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screening_verification_deadline import (
    DeadlineProvenance,
    FinalizerState,
    NotApplicableReason,
    ScreeningVerificationDeadlineView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_screening_decisions import (
    screening_decision_record_view,
)
from ditto.api_server.review_timeout_finalizer import configured_finalizer_mode
from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningDecisionRecord,
    ScreeningQuarantine,
)
from ditto_screening_protocol import (
    NON_DECISIVE_REASON_CODES,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    FailureDomain,
    SourceReviewFinding,
    failure_domain_for_reason_code,
)

router = APIRouter(prefix="/admin/screening-verification-deadline", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_POLICY = PUBLISHED_REVIEW_TIMEOUT_POLICY
# The window's only source today; see DeadlineProvenance's docstring.
_DEADLINE_PROVENANCE: DeadlineProvenance = "shipped_default"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parsed_finding(
    quarantine: ScreeningQuarantine | None,
) -> SourceReviewFinding | None:
    if quarantine is None or not isinstance(quarantine.finding, dict):
        return None
    try:
        return SourceReviewFinding.model_validate(quarantine.finding)
    except ValueError:
        return None


async def _retry_evidence(
    session: AsyncSession, *, agent_id: UUID
) -> tuple[int, list[str]]:
    """Attempts run under the strict policy and the distinct workers that ran them.

    Deliberately re-implemented here rather than imported from
    ``review_timeout_finalizer._retry_evidence``: that helper is private to
    the finalizer module (issue #2100 is an additive read layer that must not
    touch finalizer internals), and the predicate itself -- every
    ``screening_attempts`` row for this agent at or above the policy's
    ``applies_from_policy_version`` -- is small enough to duplicate exactly
    rather than risk coupling a read endpoint to finalizer refactors.

    Returns ``(attempt_count, sorted distinct screener hotkeys)``.
    """
    attempts = list(
        await session.scalars(
            select(ScreeningAttempt.screener_hotkey).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.policy_version >= _POLICY.applies_from_policy_version,
            )
        )
    )
    return len(attempts), sorted(set(attempts))


@router.get("/{agent_id}", response_model=ScreeningVerificationDeadlineView)
async def get_screening_verification_deadline(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> ScreeningVerificationDeadlineView:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    now = datetime.now(UTC)
    mode = configured_finalizer_mode()

    quarantine = await session.scalar(
        select(ScreeningQuarantine)
        .where(ScreeningQuarantine.agent_id == agent_id)
        .order_by(
            ScreeningQuarantine.created_at.desc(),
            ScreeningQuarantine.quarantine_id.desc(),
        )
        .limit(1)
    )

    latest_quarantine_decision: ScreeningDecisionRecord | None = None
    if quarantine is not None:
        latest_quarantine_decision = await session.scalar(
            select(ScreeningDecisionRecord)
            .where(ScreeningDecisionRecord.quarantine_id == quarantine.quarantine_id)
            .order_by(
                ScreeningDecisionRecord.decided_at.desc(),
                ScreeningDecisionRecord.decision_id.desc(),
            )
            .limit(1)
        )
    # A decision tied to the latest quarantine is only "finalized" -- and only
    # authoritative over the fields below -- when the deadline finalizer
    # itself wrote it. An operator manual clear/reject, a pre-v13 quarantine,
    # or a finding hold can (in principle, and defensively even if no writer
    # does this today) carry a decision tied to the same quarantine_id without
    # ever having gone through the timeout finalizer; treating that as
    # "finalized" would fabricate a verification_deadline for a row the
    # finalizer never touched.
    quarantine_decision: ScreeningDecisionRecord | None = (
        latest_quarantine_decision
        if latest_quarantine_decision is not None
        and latest_quarantine_decision.outcome == REVIEW_TIMED_OUT_OUTCOME
        else None
    )

    attempt: ScreeningAttempt | None = None
    if quarantine is not None:
        attempt = await session.get(ScreeningAttempt, quarantine.attempt_id)
    else:
        # No quarantine at all (never held, or the hold already fully
        # resolved by an operator through a path that writes no decision
        # record): still surface the most recent attempt for context. This
        # never feeds finalizer_state or `decision` -- only a decision the
        # finalizer itself wrote for the CURRENT hold can make finalizer_state
        # "finalized", and `decision` mirrors that same invariant exactly.
        attempt = await session.scalar(
            select(ScreeningAttempt)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(ScreeningAttempt.started_at.desc())
            .limit(1)
        )

    is_operator_finding_hold = quarantine is not None and (
        quarantine.finding is not None or quarantine.finding_digest is not None
    )
    has_active_quarantine = quarantine is not None and quarantine.status == "active"

    policy_version: int | None = None
    if quarantine_decision is not None:
        policy_version = quarantine_decision.policy_version
    elif quarantine is not None:
        policy_version = quarantine.policy_version
    elif attempt is not None:
        policy_version = attempt.policy_version
    policy_covered_by_finalizer = (
        policy_version is not None
        and policy_version >= _POLICY.applies_from_policy_version
    )

    reason_code = (
        quarantine.reason_code
        if quarantine is not None
        else (attempt.reason_code if attempt is not None else None)
    )

    # --- finalizer_state -----------------------------------------------
    finalizer_state: FinalizerState
    not_applicable_reason: NotApplicableReason | None = None
    if quarantine_decision is not None:
        finalizer_state = "finalized"
    elif mode == "off":
        finalizer_state = "not_configured"
        not_applicable_reason = "finalizer_mode_off"
    elif not has_active_quarantine:
        finalizer_state = "not_configured"
        not_applicable_reason = "no_active_quarantine"
    elif is_operator_finding_hold:
        finalizer_state = "not_configured"
        not_applicable_reason = "operator_finding_hold"
    elif not policy_covered_by_finalizer:
        finalizer_state = "not_configured"
        not_applicable_reason = "policy_version_not_covered"
    else:
        # has_active_quarantine is True here, so quarantine is not None.
        assert quarantine is not None
        if quarantine.reason_code not in NON_DECISIVE_REASON_CODES:
            finalizer_state = "not_configured"
            not_applicable_reason = "reason_code_not_covered"
        else:
            deadline = _aware(quarantine.created_at) + _POLICY.max_verification_window
            finalizer_state = "ready" if now >= deadline else "pending"

    # --- verification window --------------------------------------------
    # Never computed (and never fabricated from the policy's recommendation
    # or an attempt's own lease deadline) when the finalizer does not apply.
    verification_window_start: datetime | None = None
    verification_deadline: datetime | None = None
    verification_deadline_provenance: DeadlineProvenance | None = None
    if finalizer_state in ("pending", "ready", "finalized") and quarantine is not None:
        verification_window_start = _aware(quarantine.created_at)
        verification_deadline = (
            verification_window_start + _POLICY.max_verification_window
        )
        verification_deadline_provenance = _DEADLINE_PROVENANCE

    # --- artifact identity -----------------------------------------------
    finding = _parsed_finding(quarantine)
    artifact_identity_verified: bool | None = None
    if quarantine_decision is not None:
        identities = (
            quarantine_decision.identities
            if isinstance(quarantine_decision.identities, dict)
            else {}
        )
        recorded_sha = identities.get("artifact_sha256")
        if isinstance(recorded_sha, str) and recorded_sha:
            artifact_identity_verified = recorded_sha == agent.sha256
    elif finding is not None:
        artifact_identity_verified = finding.artifact_sha256 == agent.sha256

    # --- failure domain and retry evidence --------------------------------
    failure_domain: FailureDomain | None = None
    required_retries: int | None = None
    recorded_retry_attempts: int | None = None
    independent_worker_hotkeys: list[str] = []
    independent_worker_count = 0
    completed_checks: list[str] | None = None
    failed_checks: list[str] | None = None

    if quarantine_decision is not None:
        # As recorded -- never recomputed.
        failure_domain = cast(FailureDomain, quarantine_decision.failure_domain)
        recorded_retry_attempts = quarantine_decision.retry_count
        independent_worker_count = quarantine_decision.independent_workers
        completed_checks = list(quarantine_decision.completed_checks or [])
        failed_checks = list(quarantine_decision.failed_checks or [])
        required_retries = _POLICY.automatic_retry_budget(failure_domain)
        # The decision keeps only the worker COUNT, not identities; the live
        # attempt history below is supplementary detail, not a correction.
        _, independent_worker_hotkeys = await _retry_evidence(
            session, agent_id=agent_id
        )
    elif (
        quarantine is not None
        and not is_operator_finding_hold
        and quarantine.reason_code in NON_DECISIVE_REASON_CODES
    ):
        failure_domain = failure_domain_for_reason_code(
            quarantine.reason_code,
            failure_provider=attempt.failure_provider if attempt is not None else None,
        )
        required_retries = _POLICY.automatic_retry_budget(failure_domain)
        recorded_retry_attempts, independent_worker_hotkeys = await _retry_evidence(
            session, agent_id=agent_id
        )
        independent_worker_count = len(independent_worker_hotkeys)

    independent_worker_requirement_met: bool | None = None
    if failure_domain is not None and _POLICY.independent_worker_required(
        failure_domain
    ):
        independent_worker_requirement_met = independent_worker_count >= 2

    return ScreeningVerificationDeadlineView(
        agent_id=agent.agent_id,
        agent_status=agent.status.value,
        artifact_sha256=agent.sha256,
        artifact_identity_verified=artifact_identity_verified,
        has_active_quarantine=has_active_quarantine,
        is_operator_finding_hold=is_operator_finding_hold,
        quarantine_id=quarantine.quarantine_id if quarantine is not None else None,
        quarantine_status=(
            cast(Literal["active", "resolved"], quarantine.status)
            if quarantine is not None
            else None
        ),
        quarantine_resolution=quarantine.resolution if quarantine is not None else None,
        attempt_id=attempt.attempt_id if attempt is not None else None,
        reason_code=reason_code,
        policy_version=policy_version,
        policy_covered_by_finalizer=policy_covered_by_finalizer,
        policy_digest=(
            quarantine_decision.identities.get("policy_digest")
            if quarantine_decision is not None
            and isinstance(quarantine_decision.identities, dict)
            else (quarantine.manifest_digest if quarantine is not None else None)
        ),
        verification_profile_digest=(
            quarantine_decision.identities.get("verification_profile_digest")
            if quarantine_decision is not None
            and isinstance(quarantine_decision.identities, dict)
            else (quarantine.review_audit_digest if quarantine is not None else None)
        ),
        verification_window_start=verification_window_start,
        verification_deadline=verification_deadline,
        verification_deadline_provenance=verification_deadline_provenance,
        failure_domain=failure_domain,
        required_retries=required_retries,
        recorded_retry_attempts=recorded_retry_attempts,
        independent_worker_hotkeys=independent_worker_hotkeys,
        independent_worker_count=independent_worker_count,
        independent_worker_requirement_met=independent_worker_requirement_met,
        completed_checks=completed_checks,
        failed_checks=failed_checks,
        finalizer_mode=mode,
        finalizer_state=finalizer_state,
        not_applicable_reason=not_applicable_reason,
        decision=(
            screening_decision_record_view(quarantine_decision)
            if quarantine_decision is not None
            else None
        ),
    )

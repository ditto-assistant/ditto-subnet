"""Policy v13 screening decision record and review-timeout policy wire models.

Backroom reads these through ``GET /admin/screening-decisions`` and
``GET /admin/screening-decisions/{agent_id}``; the published retry/deadline
policy is also folded into the screener-policy activation view so an operator
scheduling an activation sees the treatment a timeout receives. Reason and
evidence text is unbounded (a bounded ``reason`` fails the OpenAPI invariant
test and would truncate audit evidence).
"""

from __future__ import annotations

from datetime import datetime
from typing import TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol import (
    DEFAULT_REVIEW_TIMEOUT_FINALIZER_MODE,
    POLICY_V13_ACTIVATION_PREREQUISITES,
    PUBLISHED_REVIEW_CAPACITY_THRESHOLDS,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    FailureDomain,
    ReviewTimeoutFinalizerMode,
    ScreeningDecisionOutcome,
    activation_ceiling_from_checklist,
)

# One source for every enumeration: the protocol Literal feeds these wire
# models, the ORM CHECK constraints and the finalizer alike.
ScreeningFailureDomain: TypeAlias = FailureDomain


class ScreeningDecisionIdentities(BaseModel):
    """The ten identities policy-v13.md binds into every decision record."""

    model_config = ConfigDict(extra="ignore")

    submission_uuid: UUID
    artifact_sha256: str
    image_digest: str | None = None
    build_configuration: str | None = None
    served_entrypoint: str | None = None
    permitted_runtime_configuration: str | None = None
    benchmark_version: int | None = None
    applied_policy_version: int
    policy_digest: str | None = None
    verification_profile_digest: str | None = None


class ScreeningDecisionRecordView(BaseModel):
    """One terminal review decision, in policy-v13.md decision-record shape."""

    model_config = ConfigDict(extra="ignore")

    decision_id: UUID
    agent_id: UUID
    attempt_id: UUID | None
    quarantine_id: UUID | None
    review_id: UUID | None
    outcome: ScreeningDecisionOutcome
    reason_codes: list[str]
    violation_proven: bool
    failure_domain: ScreeningFailureDomain
    retry_count: int = Field(ge=0)
    independent_workers: int = Field(ge=0)
    policy_version: int = Field(ge=1)
    identities: ScreeningDecisionIdentities
    review_scope: str | None
    completed_checks: list[str]
    failed_checks: list[str]
    opaque_components: list[str]
    evidence_references: list[str]
    evidence_type: str | None
    limitations: list[str]
    public_reason: str
    reviewer: str
    decided_at: datetime
    supersedes_decision: UUID | None
    operator_override: dict[str, object] | None
    precedent_weight: bool
    retry_grant_id: UUID | None
    # Derived: a timeout is not one of the policy's two review outcomes.
    is_verification_failure: bool
    no_fault: bool


class ReviewTimeoutPolicyView(BaseModel):
    """The published retry/deadline procedure and the timeout treatment."""

    model_config = ConfigDict(extra="ignore")

    artifact_failure_retries: int
    provider_failure_retries: int
    platform_failure_retries: int
    independent_worker_required_for_platform_provider_failure: bool
    max_verification_window_hours: int
    applies_from_policy_version: int
    terminal_outcome: ScreeningDecisionOutcome
    ban_on_timeout: bool
    precedent_weight_on_timeout: bool
    automatic_priority_rescreen_on_recovery: bool
    no_fault_retry_grant_on_timeout: bool


class ReviewCapacityThresholdsView(BaseModel):
    """Published review-capacity, completion, latency and backlog thresholds."""

    model_config = ConfigDict(extra="ignore")

    min_healthy_source_review_workers: int
    min_completion_rate: float
    max_fail_open_rate: float
    max_p95_review_latency_hours: int
    max_backlog_multiplier: int


class ActivationPrerequisiteView(BaseModel):
    """One published policy-v13 activation prerequisite and its verification."""

    model_config = ConfigDict(extra="ignore")

    key: str
    summary: str
    verified: bool
    evidence: str | None = None


class ActivationCeilingView(BaseModel):
    """Why the activation ceiling sits where it does."""

    model_config = ConfigDict(extra="ignore")

    activation_ceiling_policy_version: int
    checklist_ceiling_policy_version: int
    prerequisites: list[ActivationPrerequisiteView]
    unverified_count: int = Field(ge=0)
    # The Platform deadline finalizer's configured posture on this build:
    # shadow logs would-be review_timed_out decisions without writing them.
    finalizer_mode: ReviewTimeoutFinalizerMode = DEFAULT_REVIEW_TIMEOUT_FINALIZER_MODE


class AdminScreeningDecisionRecordResponse(BaseModel):
    """Every decision recorded for one agent, newest first."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    agent_status: str | None
    latest: ScreeningDecisionRecordView | None
    decisions: list[ScreeningDecisionRecordView]
    review_timeout_policy: ReviewTimeoutPolicyView
    review_capacity_thresholds: ReviewCapacityThresholdsView


class AdminScreeningDecisionList(BaseModel):
    """Paged decision records across agents with outcome counts."""

    model_config = ConfigDict(extra="ignore")

    items: list[ScreeningDecisionRecordView]
    count: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    outcome: ScreeningDecisionOutcome | None
    outcome_counts: dict[str, int]
    review_timeout_policy: ReviewTimeoutPolicyView
    review_capacity_thresholds: ReviewCapacityThresholdsView


def review_timeout_policy_view() -> ReviewTimeoutPolicyView:
    policy = PUBLISHED_REVIEW_TIMEOUT_POLICY
    return ReviewTimeoutPolicyView(
        artifact_failure_retries=policy.artifact_failure_retries,
        provider_failure_retries=policy.provider_failure_retries,
        platform_failure_retries=policy.platform_failure_retries,
        independent_worker_required_for_platform_provider_failure=(
            policy.independent_worker_required_for_platform_provider_failure
        ),
        max_verification_window_hours=policy.max_verification_window_hours,
        applies_from_policy_version=policy.applies_from_policy_version,
        terminal_outcome=policy.terminal_outcome,
        ban_on_timeout=policy.ban_on_timeout,
        precedent_weight_on_timeout=policy.precedent_weight_on_timeout,
        automatic_priority_rescreen_on_recovery=(
            policy.automatic_priority_rescreen_on_recovery
        ),
        no_fault_retry_grant_on_timeout=policy.no_fault_retry_grant_on_timeout,
    )


def review_capacity_thresholds_view() -> ReviewCapacityThresholdsView:
    thresholds = PUBLISHED_REVIEW_CAPACITY_THRESHOLDS
    return ReviewCapacityThresholdsView(
        min_healthy_source_review_workers=(
            thresholds.min_healthy_source_review_workers
        ),
        min_completion_rate=thresholds.min_completion_rate,
        max_fail_open_rate=thresholds.max_fail_open_rate,
        max_p95_review_latency_hours=thresholds.max_p95_review_latency_hours,
        max_backlog_multiplier=thresholds.max_backlog_multiplier,
    )


def activation_ceiling_view(
    *,
    finalizer_mode: ReviewTimeoutFinalizerMode = DEFAULT_REVIEW_TIMEOUT_FINALIZER_MODE,
) -> ActivationCeilingView:
    prerequisites = [
        ActivationPrerequisiteView(
            key=item.key,
            summary=item.summary,
            verified=item.verified,
            evidence=item.evidence,
        )
        for item in POLICY_V13_ACTIVATION_PREREQUISITES
    ]
    return ActivationCeilingView(
        activation_ceiling_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
        checklist_ceiling_policy_version=activation_ceiling_from_checklist(),
        prerequisites=prerequisites,
        unverified_count=sum(1 for item in prerequisites if not item.verified),
        finalizer_mode=finalizer_mode,
    )

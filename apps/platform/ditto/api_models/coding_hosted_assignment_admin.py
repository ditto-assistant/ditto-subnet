"""Admin wire models for creating, cancelling and reading hosted-v2 assignments."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from ditto.api_models.coding_control_plane import CodingHostedOperationState
from ditto.api_models.coding_evaluation import CodingEvaluationModel, Sha256

ValidatorHotkey = Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]


class AdminHostedAssignmentSubject(CodingEvaluationModel):
    """Operator-chosen inputs; artifact and release digests are derived server-side."""

    agent_id: UUID
    release_row_id: UUID
    catalog_index: Annotated[int, Field(strict=True, ge=0, le=249)]
    validator_hotkey: ValidatorHotkey
    policy_sha256: Sha256
    execution_profile_sha256: Sha256
    grading_profile_sha256: Sha256
    max_patch_bytes: Annotated[int, Field(strict=True, ge=1, le=128 << 20)]


class AdminHostedAssignmentPreviewRequest(AdminHostedAssignmentSubject):
    lease_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 900


class AdminHostedAssignmentCreateRequest(AdminHostedAssignmentSubject):
    evaluation_id: UUID
    attempt_id: UUID
    deadline_unix: Annotated[int, Field(strict=True, gt=0)]
    confirmed_assignment_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminHostedAssignmentPlan(CodingEvaluationModel):
    evaluation_id: UUID
    attempt_id: UUID
    deadline_unix: int
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    registration_sha256: Sha256
    bench_version: int
    certification_row_id: UUID
    schedule_sha256: Sha256
    selection_sha256: Sha256
    selection: dict[str, Any]
    assignment_sha256: Sha256
    authority: dict[str, Any]
    confirmation: str
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


class AdminHostedAssignmentCreated(AdminHostedAssignmentPlan):
    authoring_grant_id: UUID
    grading_grant_id: UUID


# --- Bounded cancellation and redacted lifecycle views -----------------------

# The single source for these vocabularies in the operator views and queries.
HostedTerminalOutcome = Literal[
    "completed", "candidate_failure", "infrastructure_failure", "integrity_failure"
]
HostedCloseReason = Literal["completed", "failed", "aborted"]
# The admin lifecycle views add ``cancelled``; the published control-plane
# projection keeps its seven states and carries a separate flag instead.
HostedAssignmentState = Literal[CodingHostedOperationState, "cancelled"]
# Same trimmed bound as the assignment registration reason.
MAX_HOSTED_REASON_LENGTH = 512


class AdminHostedAssignmentCancelRequest(CodingEvaluationModel):
    """Cancel one unstarted assignment; the confirmation binds its exact digest."""

    expected_assignment_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not 8 <= len(value) <= MAX_HOSTED_REASON_LENGTH:
            raise ValueError(
                f"reason must contain 8 to {MAX_HOSTED_REASON_LENGTH} characters"
            )
        return value

    @field_validator("actor")
    @classmethod
    def actor_is_present(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class AdminHostedAssignmentCancellationRecord(CodingEvaluationModel):
    assignment_sha256: Sha256
    prior_state: Literal["pending_admission", "admitted"]
    reason: str
    actor: str
    cancelled_at: datetime


class AdminHostedAssignmentSummary(CodingEvaluationModel):
    evaluation_id: UUID
    attempt_id: UUID
    release_row_id: UUID
    registration_sha256: Sha256
    agent_id: UUID
    validator_hotkey: ValidatorHotkey
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    assignment_sha256: Sha256
    state: HostedAssignmentState
    created_at: datetime
    expires_at: datetime
    admitted_at: datetime | None
    started_at: datetime | None
    cancelled_at: datetime | None
    closed_at: datetime | None
    close_reason: HostedCloseReason | None
    terminal_outcome: HostedTerminalOutcome | None
    acknowledged: bool
    registered_actor: str
    registered_reason: str
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


class AdminHostedAssignmentList(CodingEvaluationModel):
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
    offset: Annotated[int, Field(ge=0)]
    observed_at: datetime
    assignments: list[AdminHostedAssignmentSummary]
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


class AdminHostedPrivateTaskStatus(CodingEvaluationModel):
    """Phase timestamps only; the private selection itself is never returned."""

    bound_at: datetime
    selection_sha256: Sha256
    frozen_at: datetime | None
    closed_at: datetime | None
    close_reason: HostedCloseReason | None


class AdminHostedTerminalStatus(CodingEvaluationModel):
    """Non-secret outcome and sealed-evidence digest; grading details stay sealed."""

    outcome: HostedTerminalOutcome
    evidence_sha256: Sha256
    reserved_at: datetime
    finalized_at: datetime | None


class AdminHostedInferenceAccounting(CodingEvaluationModel):
    """Grant limits and ledger totals.

    ``charged_*`` counts settled usage plus the full ceiling of every reserved or
    uncertain request, which is what the grant budget enforces. ``settled_*``
    counts provider-settled usage only. ``verified`` is true only once the grant
    is revoked with no reserved or uncertain request left.
    """

    policy_sha256: Sha256
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    request_limit: Annotated[int, Field(ge=1)]
    prompt_token_limit: Annotated[int, Field(ge=1)]
    completion_token_limit: Annotated[int, Field(ge=1)]
    cost_usd_micros_limit: Annotated[int, Field(ge=1)]
    request_count: Annotated[int, Field(ge=0)]
    reserved_count: Annotated[int, Field(ge=0)]
    settled_count: Annotated[int, Field(ge=0)]
    uncertain_count: Annotated[int, Field(ge=0)]
    charged_prompt_tokens: Annotated[int, Field(ge=0)]
    charged_completion_tokens: Annotated[int, Field(ge=0)]
    charged_cost_usd_micros: Annotated[int, Field(ge=0)]
    settled_prompt_tokens: Annotated[int, Field(ge=0)]
    settled_completion_tokens: Annotated[int, Field(ge=0)]
    settled_cost_usd_micros: Annotated[int, Field(ge=0)]
    verified: bool


class AdminHostedResultDelivery(CodingEvaluationModel):
    result_sha256: Sha256
    delivered_at: datetime
    acknowledged_at: datetime | None


class AdminHostedAssignmentDetail(AdminHostedAssignmentSummary):
    observed_at: datetime
    deadline_unix: int
    selection_sha256: Sha256
    policy_sha256: Sha256
    execution_profile_sha256: Sha256
    grading_profile_sha256: Sha256
    admission_request_sha256: Sha256 | None
    cancellable: bool
    cancellation: AdminHostedAssignmentCancellationRecord | None
    private_task: AdminHostedPrivateTaskStatus | None
    authoring_evidence_reserved_at: datetime | None
    authoring_evidence_finalized_at: datetime | None
    grading_claimed_at: datetime | None
    terminal: AdminHostedTerminalStatus | None
    inference: AdminHostedInferenceAccounting | None
    delivery_count: Annotated[int, Field(ge=0)]
    acknowledged_count: Annotated[int, Field(ge=0)]
    deliveries: Annotated[list[AdminHostedResultDelivery], Field(max_length=20)]
    deliveries_truncated: bool


class AdminHostedAssignmentCancelled(CodingEvaluationModel):
    idempotent: bool
    private_task_closed: bool
    cancellation: AdminHostedAssignmentCancellationRecord
    assignment: AdminHostedAssignmentDetail
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False

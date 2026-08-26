"""Private Backroom/operator models for screening quarantine management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ditto.api_models.screener import (
    ScreenEvidenceItem,
    ScreenReviewAudit,
    SourceReviewFinding,
)
from ditto.api_models.screener_review_settings import AdminShadowReviewObservation

QuarantineResolution = Literal["release", "rescreen", "reject"]
DisputeResolution = Literal["release", "uphold"]


class AdminQuarantineResolutionEvent(BaseModel):
    resolution: QuarantineResolution
    reason: str
    actor: str
    created_at: datetime


class AdminQuarantineItem(BaseModel):
    quarantine_id: UUID
    agent_id: UUID
    attempt_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None = None
    """Coldkey that paid for this evaluation, from ``evaluation_payments``.

    Null for legacy/test agents with no payment row: unknown, not absent.
    Payment-time provenance, not on-chain metagraph ownership, and miners
    routinely pay from several coldkeys — a match is one signal of common
    control, a mismatch is not evidence of different operators. Use
    ``GET /admin/miner-owners/{key}`` for the full linked footprint.
    """
    agent_name: str
    agent_version: int | None = None
    artifact_sha256: str
    policy_version: int
    manifest_digest: str
    finding_digest: str | None
    reason_code: str
    review_audit_digest: str | None = None
    review_audit: ScreenReviewAudit | None = None
    evidence: list[ScreenEvidenceItem] | None
    finding: SourceReviewFinding | None
    finding_verified: bool
    """True iff ``finding`` is present and its canonical digest equals the
    ``finding_digest`` bound into the screener's signed verdict."""

    status: Literal["active", "resolved"]
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    resolution: QuarantineResolution | None
    resolution_reason: str | None
    resolution_history: list[AdminQuarantineResolutionEvent] = Field(
        default_factory=list
    )


class AdminQuarantineList(BaseModel):
    items: list[AdminQuarantineItem]
    count: int


class AdminQuarantineResolveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resolution: QuarantineResolution
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]


class AdminQuarantineResolveResponse(BaseModel):
    quarantine: AdminQuarantineItem
    agent_status: str


class AdminScreeningDisputeItem(BaseModel):
    dispute_id: UUID
    agent_id: UUID
    quarantine_id: UUID
    miner_hotkey: str
    agent_name: str
    agent_version: int | None
    artifact_sha256: str
    message: str
    status: Literal["pending", "resolved"]
    created_at: datetime
    original_reason: str | None
    resolved_at: datetime | None
    resolved_by: str | None
    resolution: DisputeResolution | None
    resolution_reason: str | None


class AdminScreeningDisputeList(BaseModel):
    items: list[AdminScreeningDisputeItem]
    count: int


class AdminScreeningDisputeResolveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resolution: DisputeResolution
    reason: Annotated[str, Field(min_length=3)]


class AdminScreeningDisputeResolveResponse(BaseModel):
    dispute: AdminScreeningDisputeItem
    agent_status: str


class AdminScreeningAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    policy_version: int
    status: Literal["running", "passed", "rejected", "failed", "expired", "quarantined"]
    screener_hotkey: str
    started_at: datetime
    deadline: datetime
    finished_at: datetime | None
    reason: str | None
    reason_code: str | None
    duplicate_of: UUID | None
    duplicate_name: str | None = None
    duplicate_version: int | None = None


class AdminScreeningImageBuild(BaseModel):
    """Kaniko/runtime telemetry for one screening image build."""

    model_config = ConfigDict(extra="ignore")

    build_id: UUID
    attempt_id: UUID
    status: str
    error_code: str | None = None
    provider: str | None = None
    provider_resource_id: str | None = None
    runtime_status: str | None = None
    runtime_error_code: str | None = None
    runtime_provider_resource_id: str | None = None
    attempt_count: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class AdminScreeningSubmission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None = None
    """Coldkey that paid for this evaluation, from ``evaluation_payments``.

    Null for legacy/test agents with no payment row: unknown, not absent.
    Payment-time provenance, not on-chain metagraph ownership, and miners
    routinely pay from several coldkeys — a match is one signal of common
    control, a mismatch is not evidence of different operators. Use
    ``GET /admin/miner-owners/{key}`` for the full linked footprint.
    """
    agent_name: str
    agent_version: int | None = None
    artifact_sha256: str
    agent_status: str
    screening_policy_version: int
    screening_reason: str | None
    screening_reason_code: str | None
    submitted_at: datetime
    attempts: list[AdminScreeningAttempt]
    image_builds: list[AdminScreeningImageBuild] = []


class AdminScreeningSubmissionList(BaseModel):
    items: list[AdminScreeningSubmission]
    count: int


class AdminScreeningFailureExample(BaseModel):
    """One currently failed or running screening row in a reason-code group."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    agent_name: str
    agent_version: int | None = None
    agent_status: str
    submitted_at: datetime


class AdminScreeningFailureGroup(BaseModel):
    """Live screening jam for one (status, reason_code) pair."""

    model_config = ConfigDict(extra="ignore")

    agent_status: str
    reason_code: str | None
    count: int
    examples: list[AdminScreeningFailureExample]


class AdminScreeningFailureSummary(BaseModel):
    """Operator view of the live screening pipeline jam, grouped by cause."""

    model_config = ConfigDict(extra="ignore")

    generated_at: datetime
    screening: int
    screening_failed: int
    groups: list[AdminScreeningFailureGroup]


class AdminScreeningRescreenRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=3)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_score_count: Annotated[int, Field(ge=0)]


class AdminScreeningRescreenResponse(BaseModel):
    agent_id: UUID
    agent_status: str


class AdminScreeningRetryNowRequest(BaseModel):
    """Compare-and-swap guards for waiving one failed attempt's backoff."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=8)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_score_count: Annotated[int, Field(ge=0)]
    expected_attempt_id: UUID


class AdminScreeningRetryNowResponse(BaseModel):
    override_id: UUID
    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    backoff_deadline: datetime
    created_at: datetime
    idempotent: bool = False


class AdminExpireRunningScreeningRequest(BaseModel):
    """Compare-and-swap guards for expiring one live screening attempt."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=8)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_score_count: Annotated[int, Field(ge=0)]
    expected_attempt_id: UUID


class AdminExpireRunningScreeningResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    expired_build_ids: list[UUID] = []
    idempotent: bool = False


REJECT_SCREENING_CONFIRMATION = "REJECT SCREENING SUBMISSION"


class AdminRejectScreeningRequest(BaseModel):
    """Compare-and-swap guards for a terminal operator screening reject."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=8)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_score_count: Annotated[int, Field(ge=0)]
    expected_attempt_id: UUID
    confirmation: Literal["REJECT SCREENING SUBMISSION"]


class AdminRejectScreeningResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    expired_build_ids: list[UUID] = []
    idempotent: bool = False


class AdminBenchmarkContractRefreshRequest(BaseModel):
    """Compare-and-swap guard for rebuilding one stale benchmark contract."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=3)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_bench_version: Annotated[int, Field(gt=2)]
    expected_dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_score_count: Annotated[int, Field(ge=0)]


class AdminBenchmarkContractRefreshDetail(BaseModel):
    """Current compare-and-swap inputs for one guarded contract repair."""

    agent_id: UUID
    agent_name: str
    agent_status: str
    artifact_sha256: str
    bench_version: int
    dataset_sha256: str | None
    score_count: int
    screening_attempt_active: bool
    refresh_allowed: bool
    blocking_reason: str | None


class AdminBenchmarkContractRefreshResponse(BaseModel):
    agent_id: UUID
    agent_status: str
    bench_version: int
    expired_ticket_count: int


class AdminScreenedImageRebuildRequest(BaseModel):
    """Compare-and-swap guard for rebuilding only the screened image."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=8)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_bench_version: Annotated[int, Field(gt=2)]
    expected_score_count: Literal[0]
    expected_image_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_image_upload_id: UUID


class AdminScreenedImageRebuildDetail(BaseModel):
    """Current guarded inputs for a build-only screened-image repair."""

    agent_id: UUID
    agent_name: str
    agent_status: str
    artifact_sha256: str
    bench_version: int
    score_count: int
    screened_image_sha256: str | None
    screened_image_upload_id: UUID | None
    screening_attempt_active: bool
    validator_ticket_active: bool
    rebuild_allowed: bool
    blocking_reason: str | None


class AdminScreenedImageRebuildResponse(BaseModel):
    agent_id: UUID
    agent_status: str
    bench_version: int
    expired_ticket_count: int


class AdminBenchmarkContractMigrationRequest(BaseModel):
    """Compare-and-swap guard for moving one zero-score v2 artifact to v3."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=3)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_source_bench_version: Literal[2]
    expected_target_bench_version: Literal[3]
    expected_source_dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_source_score_count: Literal[0]
    expected_target_score_count: Literal[0]


class AdminBenchmarkContractMigrationDetail(BaseModel):
    """Current guarded inputs for one zero-score v2-to-v3 migration."""

    agent_id: UUID
    agent_name: str
    agent_status: str
    artifact_sha256: str
    source_bench_version: int
    target_bench_version: int | None
    source_dataset_sha256: str | None
    target_dataset_sha256: str | None
    source_score_count: int
    target_score_count: int
    screening_attempt_active: bool
    validator_run_active: bool
    migration_allowed: bool
    blocking_reason: str | None


class AdminBenchmarkContractMigrationResponse(BaseModel):
    agent_id: UUID
    agent_status: str
    source_bench_version: int
    target_bench_version: int
    target_dataset_sha256: str
    expired_ticket_count: int


class AdminBenchmarkQualificationRequest(BaseModel):
    """Compare-and-swap guard for qualifying a scored rolling contender."""

    model_config = ConfigDict(extra="ignore")

    reason: Annotated[str, Field(min_length=3)]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_rollout_id: UUID
    expected_total_score_count: Annotated[int, Field(ge=0)]
    expected_source_score_count: Annotated[int, Field(ge=0)]
    expected_target_score_count: Annotated[int, Field(ge=0)]


class AdminBenchmarkQualificationDetail(BaseModel):
    agent_id: UUID
    agent_name: str
    agent_status: str
    artifact_sha256: str
    rollout_id: UUID | None
    source_bench_version: int | None
    target_bench_version: int | None
    currently_top_five: bool
    rollout_member: bool
    target_dataset_sha256: str | None
    total_score_count: int
    source_score_count: int
    target_score_count: int
    screening_attempt_active: bool
    validator_run_active: bool
    qualification_allowed: bool
    blocking_reason: str | None


class AdminBenchmarkQualificationResponse(BaseModel):
    agent_id: UUID
    agent_status: str
    rollout_id: UUID
    target_bench_version: int
    target_dataset_sha256: str
    rollout_member: Literal[True] = True
    screening_queued: bool


class AdminQuarantineAgentContext(BaseModel):
    """Submission metadata an operator needs while judging a quarantine."""

    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None = None
    """Coldkey that paid for this evaluation, from ``evaluation_payments``.

    Null for legacy/test agents with no payment row: unknown, not absent.
    Payment-time provenance, not on-chain metagraph ownership, and miners
    routinely pay from several coldkeys — a match is one signal of common
    control, a mismatch is not evidence of different operators. Use
    ``GET /admin/miner-owners/{key}`` for the full linked footprint.
    """
    agent_name: str
    artifact_sha256: str
    agent_status: str
    size_bytes: int | None
    submitted_at: datetime
    screening_policy_version: int
    screening_reason: str | None


class AdminMinerQuarantineSummary(BaseModel):
    """One prior quarantine from the same miner, with its resolution."""

    quarantine_id: UUID
    agent_id: UUID
    agent_name: str
    reason_code: str
    status: Literal["active", "resolved"]
    resolution: QuarantineResolution | None
    resolution_reason: str | None
    created_at: datetime
    resolved_at: datetime | None


class AdminMinerContext(BaseModel):
    """The submitting miner's track record across all submissions."""

    miner_hotkey: str
    miner_coldkeys: list[str] = Field(default_factory=list)
    """Every payment-time coldkey ever recorded for this hotkey.

    Usually one; more than one means the hotkey's uploads were funded from
    several coldkeys, which is ordinary miner behaviour and not by itself
    suspicious. The counts below are keyed on the hotkey alone, so an operator
    running several hotkeys shows a fragmented record here — resolve the whole
    footprint with ``GET /admin/miner-owners/{key}``.
    """
    total_submissions: int
    quarantine_count: int
    released_count: int
    rescreened_count: int
    rejected_count: int
    recent_quarantines: list[AdminMinerQuarantineSummary]


class AdminArtifactDuplicate(BaseModel):
    """Another submission whose artifact matches this one."""

    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None = None
    """Coldkey that paid for this evaluation, from ``evaluation_payments``.

    Null for legacy/test agents with no payment row: unknown, not absent.
    Payment-time provenance, not on-chain metagraph ownership, and miners
    routinely pay from several coldkeys — a match is one signal of common
    control, a mismatch is not evidence of different operators. Use
    ``GET /admin/miner-owners/{key}`` for the full linked footprint.
    """
    agent_name: str
    agent_status: str
    submitted_at: datetime
    match: Literal["identical_artifact", "identical_normalized_source"]
    same_owner: bool = False
    """True when this duplicate shares the reviewed submission's hotkey, or its
    payment coldkey. False covers both "provably someone else" and "no payment
    record to compare", so it is a positive signal only when true."""


class AdminDuplicateSummary(BaseModel):
    """Authoritative duplicate counts, independent of the bounded sample."""

    total: int
    cross_miner: int
    same_miner: int
    cross_owner: int
    same_owner: int
    sample_truncated: bool


class AdminQuarantineContext(BaseModel):
    """Everything the review console shows for one quarantine decision."""

    quarantine: AdminQuarantineItem
    agent: AdminQuarantineAgentContext
    attempts: list[AdminScreeningAttempt]
    miner: AdminMinerContext
    duplicates: list[AdminArtifactDuplicate]
    """A bounded sample (at most 20); use ``duplicate_summary`` for counts."""

    duplicate_summary: AdminDuplicateSummary
    shadow_review: AdminShadowReviewObservation | None = None
    """The L2/L3 agentic source review for this quarantine's attempt, when one
    was recorded. **Non-authoritative:** shadow mode cannot quarantine, reject
    or ban, so this is advisory signal an operator weighs against the L1
    finding --- most usefully when the two disagree. Null is the normal case:
    the reviewer runs only while shadow mode is on, and no quarantine raised
    before it existed has a row."""


class AdminQuarantineBatchContextRequest(BaseModel):
    """Bounded context fan-out for queue workbenches and MCP clients."""

    model_config = ConfigDict(extra="ignore")

    quarantine_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)]


class AdminQuarantineBatchContextResult(BaseModel):
    quarantine_id: UUID
    context: AdminQuarantineContext | None = None
    error: str | None = None


class AdminQuarantineBatchContextResponse(BaseModel):
    items: list[AdminQuarantineBatchContextResult]
    count: int


class AdminQuarantineBatchDecision(BaseModel):
    """One guarded decision in a separately previewed batch."""

    model_config = ConfigDict(extra="ignore")

    quarantine_id: UUID
    expected_agent_id: UUID
    expected_artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    resolution: QuarantineResolution
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]


class AdminQuarantineBatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decisions: Annotated[
        list[AdminQuarantineBatchDecision], Field(min_length=1, max_length=50)
    ]


class AdminQuarantineBatchPreviewItem(BaseModel):
    quarantine_id: UUID
    agent_id: UUID | None = None
    agent_name: str | None = None
    artifact_sha256: str | None = None
    resolution: QuarantineResolution
    reason: str
    disposition: Literal["ready", "already_applied", "conflict", "not_found"]
    resulting_agent_status: str | None = None
    message: str


class AdminQuarantineBatchPreviewResponse(BaseModel):
    preview_token: str
    expires_at: datetime
    items: list[AdminQuarantineBatchPreviewItem]
    ready_count: int
    already_applied_count: int
    blocked_count: int


class AdminQuarantineBatchExecuteRequest(AdminQuarantineBatchPreviewRequest):
    preview_token: Annotated[str, Field(min_length=32, max_length=256)]
    confirmed: Literal[True]


class AdminQuarantineBatchExecuteItem(BaseModel):
    quarantine_id: UUID
    status: Literal["applied", "already_applied", "failed"]
    agent_status: str | None = None
    message: str


class AdminQuarantineBatchExecuteResponse(BaseModel):
    items: list[AdminQuarantineBatchExecuteItem]
    applied_count: int
    already_applied_count: int
    failed_count: int


class AdminSourceFileEntry(BaseModel):
    path: str
    bytes: int


class AdminOpaqueBlobEntry(BaseModel):
    """A member the text reader cannot show; a natural hiding place."""

    path: str
    bytes: int
    reason: Literal["oversized", "non_utf8"]


class AdminSourceListing(BaseModel):
    agent_id: UUID
    artifact_sha256: str
    file_count: int
    files: list[AdminSourceFileEntry]
    opaque_blobs: list[AdminOpaqueBlobEntry]
    opaque_total: int
    """Total unreadable members found; ``opaque_blobs`` shows at most 128."""

    truncated: bool


class AdminSourceLine(BaseModel):
    line: int
    text: str


class AdminSourceExcerpt(BaseModel):
    agent_id: UUID
    path: str
    total_lines: int
    start_line: int
    end_line: int
    lines: list[AdminSourceLine]


class AdminSourceSearchMatch(BaseModel):
    path: str
    line: int
    text: str
    """The matching line, clipped to 500 characters like every excerpt line."""

    context_before: list[AdminSourceLine] = Field(default_factory=list)
    context_after: list[AdminSourceLine] = Field(default_factory=list)


class AdminSourceSearchResult(BaseModel):
    """Regex/literal hits across one submission's readable members."""

    agent_id: UUID
    artifact_sha256: str
    pattern: str
    mode: Literal["regex", "literal"]
    path_glob: str | None = None
    matches: list[AdminSourceSearchMatch]
    match_count: int
    """Matches the scan found. A lower bound when ``truncated``."""

    returned: int
    limit: int
    offset: int
    has_more: bool
    """True when matches exist past this page — the paging signal."""

    files_searched: int
    files_matched: int
    opaque_skipped: int
    """Members never searched because they are binary or oversized.

    The same blobs ``AdminSourceListing.opaque_blobs`` names: a search cannot
    clear them, so their count travels with every result.
    """

    truncated: bool
    """True when the scan stopped at its match cap; totals are lower bounds."""


class AdminStarterKitProvenance(BaseModel):
    """Which starter-kit revision the submission was diffed against."""

    source: str
    revision: str
    commit_set_sha256: str
    commit_count: int


class AdminBaselineDiffFile(BaseModel):
    path: str
    status: Literal["added", "removed", "modified", "identical"]
    candidate_lines: int
    reference_lines: int
    added_lines: int
    removed_lines: int
    similarity: float
    normalized_identical: bool
    # True when this content is starter-kit code at ANY revision in the pinned
    # lineage, not merely identical to the tip. A miner who forked an older
    # commit ships kit files that differ from the tip but are still not theirs.
    stock_kit: bool


class AdminBaselineDiffManifest(BaseModel):
    agent_id: UUID
    artifact_sha256: str
    baseline: AdminStarterKitProvenance
    files: list[AdminBaselineDiffFile]
    file_count: int
    identical_count: int
    modified_count: int
    added_count: int
    removed_count: int
    stock_kit_count: int
    custom_file_count: int
    # Lines that are neither baseline code nor kit code at any revision: the
    # size of the surface a reviewer actually has to read.
    custom_added_lines: int
    # True when the submission's paths were realigned by stripping one wrapping
    # directory so they line up with the kit layout.
    path_aligned: bool
    truncated: bool


class AdminBaselineDiffFileDetail(BaseModel):
    agent_id: UUID
    path: str
    candidate_present: bool
    reference_present: bool
    identical: bool
    stock_kit: bool
    diff_lines: list[str]
    truncated: bool


class AdminValidatorAssignment(BaseModel):
    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    validator_hotkey: str
    issued_at: datetime
    deadline: datetime
    bench_version: int
    attempt_count: int
    score_count: int
    provisional_composite: float | None
    slot_id: str = "slot-0"
    purpose: Literal["legacy_unclassified", "canonical_quorum", "continual_retest"] = (
        "legacy_unclassified"
    )
    agent_status: str | None = None
    first_reported_at: datetime | None = None


class AdminValidatorAssignmentList(BaseModel):
    items: list[AdminValidatorAssignment]
    count: int


class AdminValidatorAssignmentReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_deadline: datetime
    reason: Annotated[str, Field(min_length=8)]


class AdminValidatorAssignmentReleaseResponse(BaseModel):
    agent_id: UUID
    validator_hotkey: str
    status: Literal["expired"]
    retry_after: datetime


__all__ = [
    "AdminArtifactDuplicate",
    "AdminBaselineDiffFile",
    "AdminBaselineDiffFileDetail",
    "AdminBaselineDiffManifest",
    "AdminBenchmarkQualificationDetail",
    "AdminBenchmarkQualificationRequest",
    "AdminBenchmarkQualificationResponse",
    "AdminDuplicateSummary",
    "AdminMinerContext",
    "AdminMinerQuarantineSummary",
    "AdminOpaqueBlobEntry",
    "AdminQuarantineAgentContext",
    "AdminQuarantineContext",
    "AdminQuarantineItem",
    "AdminQuarantineList",
    "AdminQuarantineResolutionEvent",
    "AdminQuarantineResolveRequest",
    "AdminQuarantineResolveResponse",
    "AdminScreeningAttempt",
    "AdminScreeningDisputeItem",
    "AdminScreeningDisputeList",
    "AdminScreeningDisputeResolveRequest",
    "AdminScreeningDisputeResolveResponse",
    "AdminScreeningFailureExample",
    "AdminScreeningFailureGroup",
    "AdminScreeningFailureSummary",
    "AdminScreeningSubmission",
    "AdminScreeningSubmissionList",
    "AdminScreeningRescreenRequest",
    "AdminScreeningRescreenResponse",
    "AdminShadowReviewObservation",
    "AdminSourceExcerpt",
    "AdminSourceFileEntry",
    "AdminSourceLine",
    "AdminSourceListing",
    "AdminStarterKitProvenance",
    "AdminValidatorAssignment",
    "AdminValidatorAssignmentList",
    "AdminValidatorAssignmentReleaseRequest",
    "AdminValidatorAssignmentReleaseResponse",
]

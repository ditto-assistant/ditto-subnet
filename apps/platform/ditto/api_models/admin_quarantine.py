"""Private Backroom/operator models for screening quarantine management."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from ditto.api_models.screener import (
    ScreenEvidenceItem,
    ScreenReviewAudit,
    SourceReviewFinding,
)
from ditto.api_models.screener_review_settings import AdminShadowReviewObservation
from ditto_screening_protocol import (
    AdjudicationCompletionReceipt,
    AdjudicationRunDiagnostic,
    SourceReviewNote,
)

QuarantineResolution = Literal["release", "rescreen", "reject"]
DisputeResolution = Literal["release", "uphold"]
DisputeKind = Literal["screening", "gate_notes"]

ResolutionReasonCode = Literal[
    "operator-released-quarantine",
    "operator-rescreened-quarantine",
    "operator-rejected-quarantine",
]

# A manual resolution is an operator *ruling* on a quarantine, so it carries a
# code of its own instead of inheriting the code the reviewed quarantine was
# opened with. Those are two different facts — "why the screener held this
# submission" and "what an operator decided about the hold" — and reusing one
# field for both is how a submission the screener cleared ends up labelled with
# the code that cleared it. The vocabularies are disjoint by construction: no
# screening-origin code starts with ``operator-``, so a bare code still says
# which of the two a reader is holding.
#
# Named for the object ruled on, not the outcome, so they cannot collide with
# ``_OPERATOR_REJECT_REASON_CODE`` (``operator-rejected-screening``) minted by
# the pre-quarantine ``/screening-submissions/{id}/reject`` route. That
# route's retry guard treats its own code as proof it already ran, so a shared
# token would make it report a quarantined submission as its own idempotent
# retry; the two routes are different actions with different preconditions and
# stay separately named.
OPERATOR_RELEASED_QUARANTINE: ResolutionReasonCode = "operator-released-quarantine"
OPERATOR_RESCREENED_QUARANTINE: ResolutionReasonCode = "operator-rescreened-quarantine"
OPERATOR_REJECTED_QUARANTINE: ResolutionReasonCode = "operator-rejected-quarantine"

RESOLUTION_REASON_CODES: dict[str, ResolutionReasonCode] = {
    "release": OPERATOR_RELEASED_QUARANTINE,
    "rescreen": OPERATOR_RESCREENED_QUARANTINE,
    "reject": OPERATOR_REJECTED_QUARANTINE,
}


def resolution_reason_code(resolution: str | None) -> ResolutionReasonCode | None:
    """The operator ruling code a ``resolution`` implies, or ``None``.

    A resolution is a ruling, so its code is a pure function of it and is
    derived at read time rather than stored — that keeps every row already in
    the database correct without a migration, and leaves no denormalized copy
    to drift. ``None`` covers the three "no ruling here" cases a read path must
    tolerate: an unresolved quarantine, a miner dispute's ``uphold`` (which
    records no ruling of its own), and any future resolution value this build
    does not know — an unknown value degrades to "no code" instead of raising.
    """
    if resolution is None:
        return None
    return RESOLUTION_REASON_CODES.get(resolution)


def review_event_resolution_reason_code(
    event_kind: str, effective_decision: str | None
) -> ResolutionReasonCode | None:
    """The operator ruling code of a review event, or ``None``.

    Only a *manual* event can carry an operator basis: an automated
    ``effective_decision='reject'`` is the screener's own verdict arriving over
    the signed screening path, and attributing it to an operator would invent a
    ruling that never happened. The event's stored ``reason_code`` is left
    alone — it is the screening-origin code the reviewed quarantine carried,
    snapshotted verbatim.
    """
    if event_kind != "manual":
        return None
    return resolution_reason_code(effective_decision)


# One-to-one with the 19 mandatory checks in docs/policy-v13.md, plus the
# conditional private package. Presence of a receipt is never a pass verdict.
MANDATORY_V13_VERIFICATION_CHECKS = (
    "archive_sha",
    "build_image_digest",
    "health",
    "ordinary_model_run",
    "tool_selection_run",
    "seed_memory_run",
    "two_user_isolation",
    "system_instruction_retention",
    "tool_revocation",
    "successful_duplicate_suppression",
    "same_tool_different_argument",
    "catalog_fidelity_reordering",
    "timeout_delivery_unknown",
    "fallback_evidence_retention",
    "response_field_long_answer",
    "refusal_uncertainty",
    "token_accounting",
    "opaque_inventory",
    "invariants_i1_i8_s1_s3",
    "private_metamorphic",
)


class AdminQuarantineResolutionEvent(BaseModel):
    resolution: QuarantineResolution
    reason: str
    actor: str
    created_at: datetime
    resolution_reason_code: ResolutionReasonCode | None = None
    """The operator ruling's own code, derived from ``resolution``: one of the
    ``operator-*-quarantine`` tokens. Never the screening code of the
    quarantine this ruling closed."""


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
    screening_reason_code: str
    """Why the screener held *this submission*: the reason code from the signed
    verdict that opened the quarantine. Screening-origin provenance, and it
    survives the hold — a resolved quarantine still reports the code it was
    opened with. It is **not** the operator's decision; read ``resolution``
    with ``resolution_reason_code`` for that."""

    @computed_field(deprecated=True)  # type: ignore[prop-decorator]
    @property
    def reason_code(self) -> str:
        """Deprecated alias for ``screening_reason_code``, kept for the rollout.

        Platform and Backroom deploy in parallel from one release, so a Backroom
        that has not been redeployed still requires this field and would reject
        every quarantine item if it disappeared. It always carries the same
        screening-origin code as ``screening_reason_code`` — never the operator's
        ruling — and is removed once the console reads only the new name.
        """
        return self.screening_reason_code

    review_audit_digest: str | None = None
    review_audit: ScreenReviewAudit | None = None
    review_notes_digest: str | None = None
    review_notes: list[SourceReviewNote] | None = None
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
    resolution_reason_code: ResolutionReasonCode | None = None
    """What the operator decided, as a code: ``operator-released-quarantine`` /
    ``operator-rescreened-quarantine`` / ``operator-rejected-quarantine``.
    Derived from ``resolution``, so it is present on every resolved quarantine
    including ones resolved before this field existed. Null while active."""
    resolution_history: list[AdminQuarantineResolutionEvent] = Field(
        default_factory=list
    )


class AdminQuarantineList(BaseModel):
    items: list[AdminQuarantineItem]
    count: int


class AdminScreeningReviewEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: UUID
    agent_id: UUID
    attempt_id: UUID
    quarantine_id: UUID | None
    resolution_id: UUID | None
    previous_event_id: UUID | None
    event_kind: Literal["automated", "manual"]
    artifact_sha256: str
    policy_version: int
    actor: str
    reviewer_model: str | None
    outcome: str
    effective_decision: str
    screening_reason_code: str | None
    """Screening-origin code, snapshotted at event time. On an ``automated``
    event it is the code the screener's verdict carried; on a ``manual`` event
    it is the code the reviewed quarantine was opened with, inherited verbatim
    and preserved. On a manual event it therefore describes the lead the
    operator ruled on, **not** the ruling — see
    ``resolution_reason_code``."""

    @computed_field(deprecated=True)  # type: ignore[prop-decorator]
    @property
    def reason_code(self) -> str | None:
        """Deprecated alias for ``screening_reason_code``, kept for the rollout.

        Same value, same screening-origin meaning, and lost as soon as the
        console reads only the new name — see ``AdminQuarantineItem.reason_code``
        for why the transition needs it.
        """
        return self.screening_reason_code

    resolution_reason_code: ResolutionReasonCode | None = None
    """The operator's own basis, derived from ``effective_decision``. Non-null
    only on a ``manual`` event: an automated ``reject`` is the screener's
    verdict arriving over the signed screening path, not an operator ruling."""
    reason: str | None
    prior_agent_status: str
    next_agent_status: str
    evidence: dict[str, object]
    created_at: datetime


class AdminScreeningReviewEventList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[AdminScreeningReviewEvent]
    count: int
    limit: int
    offset: int


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
    kind: DisputeKind = "screening"
    """``screening`` appeals a rejected quarantine (release re-evaluates the
    submission); ``gate_notes`` appeals cited bench v13+ gate notes on a scored
    submission (either resolution only records the verdict)."""
    quarantine_id: UUID | None
    """The appealed quarantine; ``None`` for a ``gate_notes`` dispute."""
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
    gate_note_ids: list[str] | None = None
    """Bench v13+ gate ``note_id`` values the miner contested, if any."""


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


class AdminScreeningFailureDiagnostic(BaseModel):
    """Sensitive, sanitized diagnostic for one exact screening attempt."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    artifact_sha256: str
    agent_status: str
    attempt_id: UUID
    policy_version: int
    attempt_status: Literal[
        "running", "passed", "rejected", "failed", "expired", "quarantined"
    ]
    started_at: datetime
    deadline: datetime
    finished_at: datetime | None
    reason: str | None
    reason_code: str | None
    private_failure_detail: Annotated[str | None, Field(max_length=4_000)] = None
    private_failure_log_tail: Annotated[str | None, Field(max_length=16_000)] = None
    l2_review_diagnostic: ScreenReviewAudit | None = None
    """Digest-verified, fixed-label L2 accounting for this exact attempt."""
    court_diagnostic: AdjudicationRunDiagnostic | None = None
    """Sanitized automated-court trace for this attempt. Null when the attempt
    has no such trace, including rows screened before the field existed."""
    court_completion_receipt: AdjudicationCompletionReceipt | None = None
    """Successful L4 timing/attribution only; null for historical completions."""


class AdminAdjudicationAttemptTelemetry(BaseModel):
    """Text-free L4 cohort row; absent telemetry stays absent."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str | None
    policy_version: int
    manifest_digest: str
    started_at: datetime
    finished_at: datetime | None
    attempt_status: str
    adjudication_decision: Literal["clear", "reject", "escalate"]
    review_settings_revision: int | None
    review_settings_checksum: str | None
    configured_model: str | None
    configured_timeout_seconds: int | None
    configured_completion_ceiling: int | None
    observed_model: str | None
    observed_provider: str | None
    observed_upstream: str | None
    failure_code: str | None
    elapsed_ms: int | None
    first_tool_call_ms: int | None = None
    first_tool_observation: Literal["stream_delta", "complete_body"] | None = None
    request_count: int | None
    request_prompt_bytes: int | None
    request_wire_bytes: int | None
    request_event_count: int | None
    prompt_tokens: int | None
    completion_tokens: int | None


class AdminAdjudicationAttemptTelemetryList(BaseModel):
    """Most recent persisted L4 decisions, including clear and reject."""

    items: list[AdminAdjudicationAttemptTelemetry]
    limit: int
    offset: int
    lookback_hours: int


class AdminScreeningVerificationReceipt(BaseModel):
    """Digest-only evidence presence, not a verified policy outcome."""

    receipt_id: UUID
    check_code: str
    evidence_sha256: str
    image_sha256: str | None
    profile_sha256: str | None
    challenge_manifest_sha256: str | None
    worker_hotkey: str
    created_at: datetime


class AdminScreeningVerificationCheck(BaseModel):
    check_code: str
    record_status: Literal[
        "not_recorded", "recorded_unverified", "mechanically_verified"
    ]
    receipt_count: int


class AdminV13PrivatePackageRegisterRequest(BaseModel):
    """Operator assertion of sealed digests; never private case bytes."""

    model_config = ConfigDict(extra="ignore")

    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pair_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clean_agent_id: UUID
    clean_attempt_id: UUID
    clean_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clean_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_hotkey: str = Field(min_length=1, max_length=120)


class AdminV13PrivatePrerequisite(BaseModel):
    code: str
    status: Literal["not_observed", "recorded_unverified", "mechanically_verified"]


class AdminV13PrivatePackageReadiness(BaseModel):
    """Prerequisite visibility only: no V13 policy pass or CLEAR status."""

    registration_status: Literal["not_registered", "registered_unverified"]
    prerequisites: list[AdminV13PrivatePrerequisite]
    clear_authorized: Literal[False] = False


class AdminScreeningVerificationReadiness(BaseModel):
    """Exact-attempt Platform receipt inventory, never a CLEAR authorization.

    `not_recorded` means there is no matching receipt in this Platform ledger;
    it does not prove the check never ran in an external system. The trusted
    screener records archive and built-image mechanical observations. Only
    those checks can be `mechanically_verified` after Platform recomputes their
    canonical digest and matches the committed artifact / verified image.
    Neither status certifies the other 17 checks or private 60-pair package.
    """

    agent_id: UUID
    artifact_sha256: str
    attempt_id: UUID
    policy_version: int
    attempt_status: str
    verified_image_sha256s: list[str] = Field(max_length=16)
    verified_image_count: int = Field(ge=0)
    verified_images_truncated: bool
    checks: list[AdminScreeningVerificationCheck]
    private_metamorphic_applicability: Literal["not_recorded"] = "not_recorded"
    private_package: AdminV13PrivatePackageReadiness | None = None
    receipts: list[AdminScreeningVerificationReceipt]
    receipt_count: int
    receipts_truncated: bool


class AdminScreeningReviewDeadlineAttempt(BaseModel):
    """One recorded attempt for the exact current artifact and policy."""

    attempt_id: UUID
    status: str
    screener_hotkey: str
    started_at: datetime
    finished_at: datetime | None
    reason_code: str | None


class AdminScreeningReviewDeadlineDiagnostic(BaseModel):
    """Read-only exact-artifact clock evidence, never a finalizer verdict.

    No deployed writer/finalizer is implied by a policy recommendation or a
    screening lease deadline. Distinct hotkeys are observed identities, not
    proof of independent workers or a completed retry requirement.
    """

    agent_id: UUID
    artifact_sha256: str
    agent_status: str
    policy_version: int = Field(ge=0)
    quarantine_id: UUID | None
    quarantine_status: str | None
    quarantine_resolution: str | None
    quarantine_attempt_id: UUID | None
    quarantine_artifact_matches: bool | None
    manifest_digest: str | None
    deadline_state: Literal["bound", "not_configured"]
    finalizer_state: Literal["not_configured"] = "not_configured"
    activation_revision: int | None
    policy_document_digest: str | None = None
    activation_actor: str | None
    activation_reason: str | None
    activated_at: datetime | None
    start_event: str | None
    window_started_at: datetime | None
    deadline_at: datetime | None
    recorded_attempts: list[AdminScreeningReviewDeadlineAttempt]
    observed_worker_hotkeys: list[str]
    required_retries: None = None
    independent_worker_count: None = None
    failure_domain: None = None
    outstanding_mandatory_checks: None = None


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
    generation: Literal["active", "all"]
    active_bench_version: int = Field(ge=1)


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
    generation: Literal["active", "all"]
    """Whether this is the current-work view or an explicit historical audit."""
    active_bench_version: int = Field(ge=1)
    """The benchmark version currently holding Platform authority."""
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
    force_full_review: bool = False
    review_settings_revision: Annotated[int | None, Field(ge=1)] = None
    confirmation: str | None = None

    @model_validator(mode="after")
    def validate_full_review_confirmation(self) -> AdminScreeningRetryNowRequest:
        full_review_confirmation = "FORCE ONE FULL SCREENING REVIEW"
        adjudicator_confirmation = "FORCE ONE FULL SCREENING REVIEW WITH ADJUDICATOR"
        if self.review_settings_revision is not None:
            if (
                not self.force_full_review
                or self.confirmation != adjudicator_confirmation
            ):
                raise ValueError(
                    "adjudicator retry requires confirmation "
                    f'"{adjudicator_confirmation}"'
                )
        elif self.force_full_review and self.confirmation != full_review_confirmation:
            raise ValueError(
                f'full review retry requires confirmation "{full_review_confirmation}"'
            )
        if not self.force_full_review and self.confirmation is not None:
            raise ValueError("confirmation is only valid for a full review retry")
        return self


class AdminScreeningRetryNowResponse(BaseModel):
    override_id: UUID
    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    backoff_deadline: datetime
    created_at: datetime
    force_full_review: bool = False
    review_settings_revision: int | None = None
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
    screening_reason_code: str
    """Why the screener held that submission. Preserved across the resolution:
    this is the lead the operator ruled on, not the ruling."""

    @computed_field(deprecated=True)  # type: ignore[prop-decorator]
    @property
    def reason_code(self) -> str:
        """Deprecated alias for ``screening_reason_code``, kept for the rollout.

        Same value, same screening-origin meaning, and lost as soon as the
        console reads only the new name — see ``AdminQuarantineItem.reason_code``
        for why the transition needs it.
        """
        return self.screening_reason_code

    status: Literal["active", "resolved"]
    resolution: QuarantineResolution | None
    resolution_reason: str | None
    resolution_reason_code: ResolutionReasonCode | None = None
    """The operator's ruling as a code, derived from ``resolution``. Null while
    there is no ruling — an active quarantine, or a miner dispute's ``uphold``."""
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
    # size of the surface a reviewer actually has to read. Summed over every
    # compared file, not only the rows ``files`` returns.
    custom_added_lines: int
    # True when the submission's paths were realigned by stripping one wrapping
    # directory so they line up with the kit layout.
    path_aligned: bool
    truncated: bool
    # Readable text files the bounded source read skipped (combined text budget
    # or file cap). They were NOT compared: they appear in no ``files`` row and
    # no count above, and ``file_count`` covers compared paths only.
    omitted_file_count: int
    # The first MAX_OMITTED_PATHS omitted paths, sorted.
    omitted_paths: list[str]
    # False whenever anything was omitted: ``custom_added_lines`` is then a
    # lower bound, never the whole custom surface.
    custom_added_lines_complete: bool


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
    purpose: Literal[
        "legacy_unclassified",
        "canonical_quorum",
        "continual_retest",
        "benchmark_canary",
    ] = "legacy_unclassified"
    agent_status: str | None = None
    first_reported_at: datetime | None = None


class AdminValidatorAssignmentList(BaseModel):
    items: list[AdminValidatorAssignment]
    count: int
    generation: Literal["active", "all"]
    active_bench_version: int


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
    "AdminScreeningFailureDiagnostic",
    "AdminAdjudicationAttemptTelemetry",
    "AdminAdjudicationAttemptTelemetryList",
    "AdminScreeningFailureGroup",
    "AdminScreeningFailureSummary",
    "AdminScreeningVerificationCheck",
    "AdminScreeningVerificationReadiness",
    "AdminScreeningReviewDeadlineAttempt",
    "AdminScreeningReviewDeadlineDiagnostic",
    "AdminScreeningVerificationReceipt",
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

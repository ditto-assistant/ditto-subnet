"""Admin contracts for durable ATH copy-review records."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ditto.api_models.screener import ScreenReviewAudit


class AdminDeferredReviewEvidence(BaseModel):
    """Public-safe trigger snapshot for a score-qualified source review."""

    mode: Literal["observe", "enforce"]
    triggers: list[
        Literal[
            "top_five",
            "composite_anomaly",
            "tool_anomaly",
            "memory_anomaly",
        ]
    ]
    rank: int | None = None
    cohort_size: int
    peer_count: int
    candidate: dict[str, float]
    thresholds: dict[str, dict[str, float]] | None = None
    screening_attempt_id: UUID | None = None
    screening_reason_code: str | None = None
    review_audit_digest: str | None = None
    review_audit: ScreenReviewAudit | None = None


class AdminCopyReviewEvidence(BaseModel):
    review_kind: Literal["copy", "benchmark_overfit", "deferred_source_review"] = "copy"
    duplicate_of: UUID | None
    reason: str | None
    policy_version: int
    fingerprint_versions: dict[str, int | str | None]
    reference_provenance: str
    backfilled: bool = False
    # Identity of the originally matched agent, so operators see WHICH
    # submission triggered the hold instead of a bare UUID. Null when the
    # matched agent row no longer exists.
    duplicate_of_name: str | None = None
    duplicate_of_version: int | None = None
    duplicate_of_hotkey: str | None = None
    duplicate_of_coldkey: str | None = None
    """Payment-time coldkey of the matched agent, so a reviewer can see whether
    the two submissions were paid for from the same coldkey without a second
    lookup. Null when the matched row is gone or carries no payment record.
    Same caveat as ``AdminCopyReviewItem.miner_coldkey``: one signal, not proof."""
    duplicate_of_submitted_at: datetime | None = None
    deferred_review: AdminDeferredReviewEvidence | None = None


class AdminCopySimilarityEvidence(BaseModel):
    candidate_version: int | str | None
    reference_version: int | str | None
    compatible: bool
    applicable: bool
    candidate_cardinality: int | None
    reference_cardinality: int | None
    jaccard: float | None
    containment: float | None
    above_threshold: bool
    decision_role: str


class AdminCopyReviewCurrentComparison(BaseModel):
    availability: Literal["available"]
    bulk_eligible: bool
    algorithm_version: str
    lexical_fingerprint_version: int
    normalized_source_fingerprint_version: str
    prompt_fingerprint_version: str
    canonical_reference_revision: str
    reference_corpus_id: str
    reference_exclusion_mode: str
    miner_exclusion_mode: str
    same_miner_excluded: bool
    chronology_direction: str
    chronology_eligible: bool
    exact_byte_match: bool
    normalized_source_match: bool
    lexical: AdminCopySimilarityEvidence
    structural: AdminCopySimilarityEvidence
    prompt: AdminCopySimilarityEvidence
    triggered: bool
    triggered_signal: str | None
    current_decision: str


class AdminCopyReviewComparisonUnavailable(BaseModel):
    """Per-row fail-closed comparison state for the embedded list form."""

    availability: Literal["unavailable"] = "unavailable"
    bulk_eligible: Literal[False] = False
    reason: str


class AdminCopyReviewItem(BaseModel):
    review_id: UUID
    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None = None
    """Coldkey that paid for this evaluation, from ``evaluation_payments``.

    Null for agents with no payment row: unknown, not absent. Payment-time
    provenance, not on-chain metagraph ownership. Miners routinely pay from
    several coldkeys, so matching this against ``original.duplicate_of_coldkey``
    is one signal of common control — a match is worth following, a mismatch is
    not evidence of different operators. ``GET /admin/miner-owners/{key}``
    resolves the wider footprint.
    """
    agent_name: str
    agent_version: int | None = None
    submitted_at: datetime
    status: Literal["pending", "resolved"]
    agent_status: str | None = None
    """Live ``agents.status`` for the held agent, carried on every row.

    A review's ``status`` and its agent's status are separate columns kept in
    step only by code discipline, and several paths can move the agent while
    leaving the review ``pending`` -- so a pending row reading ``scored`` here
    is a stranded hold, not a queue entry, and the difference decides whether
    ``resolve`` will even be accepted. Carrying it means an operator never has
    to reconcile a queue listing against a second per-agent lookup. Nullable
    only for wire compatibility with consumers that predate the field.
    """

    opened_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution: Literal["clear", "reject"] | None = None
    resolution_reason: str | None = None
    original: AdminCopyReviewEvidence
    # Populated only when the list is requested with
    # ``include=current_comparison``; None otherwise (and on the detail and
    # resolve responses, whose consumers use the dedicated endpoint).
    current_comparison: (
        AdminCopyReviewCurrentComparison | AdminCopyReviewComparisonUnavailable | None
    ) = None


class AdminCopyReviewList(BaseModel):
    items: list[AdminCopyReviewItem]
    count: int
    limit: int
    offset: int
    review_kind: (
        Literal["copy", "benchmark_overfit", "deferred_source_review"] | None
    ) = None
    """Echo of the applied ``review_kind`` filter; ``None`` means every kind."""

    generation: Literal["active", "rollout", "history", "all"]
    active_bench_version: int = Field(ge=1)
    rollout_bench_version: int | None = Field(default=None, ge=1)


class AdminCopyReviewAction(BaseModel):
    action: Literal["reopen", "clear", "reject"]
    reason: str
    actor: str
    created_at: datetime
    previous_status: str | None = None
    artifact_sha256: str | None = None
    score_count: int | None = None


class AdminCopyReviewAudit(BaseModel):
    """Operator audit context for one durable ATH hold."""

    review: AdminCopyReviewItem
    agent_status: str
    held_artifact_sha256: str | None = None
    held_score_count: int | None = None
    previous_status: str | None = None
    opened_by: str | None = None
    action_history: list[AdminCopyReviewAction] = Field(default_factory=list)


class AdminSourceDiffFile(BaseModel):
    path: str
    status: Literal["added", "removed", "modified", "identical"]
    candidate_lines: int
    reference_lines: int
    added_lines: int
    removed_lines: int
    similarity: float
    # Identical after comments/whitespace are canonicalized — a reformatted or
    # re-commented copy of the same code even when the raw text differs.
    normalized_identical: bool


class AdminSourceDiffManifest(BaseModel):
    agent_id: UUID
    reference_agent_id: UUID
    candidate_sha256: str
    reference_sha256: str
    files: list[AdminSourceDiffFile]
    file_count: int
    identical_count: int
    modified_count: int
    added_count: int
    removed_count: int
    # True when more files exist than the manifest bound returns; file_count
    # still reflects the real total so the omission is never silent.
    truncated: bool


class AdminSourceDiffFileDetail(BaseModel):
    agent_id: UUID
    reference_agent_id: UUID
    path: str
    candidate_present: bool
    reference_present: bool
    identical: bool
    diff_lines: list[str]
    truncated: bool


class AdminCopyReviewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # release/ban remain accepted for Backroom #20 wire compatibility.
    resolution: Literal["clear", "reject", "release", "ban"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]


class AdminCopyReviewOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_sha256: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-f]{64}$")
    ]
    expected_score_count: Annotated[int, Field(ge=0)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]


class AdminCopyReviewOpenResponse(BaseModel):
    review: AdminCopyReviewItem
    agent_status: str
    idempotent: bool
    reopened: bool


class AdminCopyReviewResolveResponse(BaseModel):
    review: AdminCopyReviewItem
    agent_status: str
    idempotent: bool

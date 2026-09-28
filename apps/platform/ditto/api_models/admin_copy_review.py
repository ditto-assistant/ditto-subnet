"""Admin contracts for durable ATH copy-review records."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ditto.api_models.screener import ScreenReviewAudit
from ditto_screening_protocol.policy_reason_codes import unpublished_violation_codes


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
    review_kind: Literal[
        "copy", "benchmark_overfit", "deferred_source_review", "anomalous_score"
    ] = "copy"
    duplicate_of: UUID | None
    reason: str | None
    """Why this submission is under review RIGHT NOW.

    For an ordinary hold this is the reason the review was opened with. For a
    hold that was reopened after its resolution was withdrawn, it is the
    reconsideration reason from the newest ``reopen`` action instead, and the
    superseded text moves to ``superseded_reason`` /
    ``superseded_resolution_reason``. A pending reconsideration must not
    advertise a withdrawn finding as the live reason, and the public activity
    projection has always followed the same rule.
    """

    reason_source: Literal["original_hold", "reconsideration"] = "original_hold"
    """Which lifecycle event ``reason`` came from. Read this before quoting it."""

    superseded_reason: str | None = None
    """The original hold reason, preserved verbatim, once superseded.

    Null unless ``reason_source`` is ``reconsideration``. Nothing is rewritten
    to produce this: ``ath_reviews.original_reason`` is immutable and the full
    action ledger stays available from the audit endpoint.
    """

    superseded_resolution: Literal["clear", "reject"] | None = None
    """The decision the reopen withdrew — historical, not an active finding."""

    superseded_resolution_reason: str | None = None
    """That withdrawn decision's own reason, recovered from the action ledger.

    The reopen NULLs ``ath_reviews.resolution_reason`` to satisfy the row's
    lifecycle constraint, so the append-only ledger is the durable copy.
    """

    superseded_at: datetime | None = None
    """When the reopen superseded the prior decision."""

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
        Literal[
            "copy", "benchmark_overfit", "deferred_source_review", "anomalous_score"
        ]
        | None
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
    evidence_references: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    policy_version: int | None = None
    violation_proven: bool | None = None


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
    model_config = ConfigDict(extra="ignore")
    path: str
    status: Literal["added", "removed", "modified", "identical", "renamed"]
    candidate_lines: int
    reference_lines: int
    added_lines: int
    removed_lines: int
    similarity: float
    # Identical after comments/whitespace are canonicalized — a reformatted or
    # re-commented copy of the same code even when the raw text differs.
    normalized_identical: bool
    # Set when status == "renamed": the reference path the candidate file was
    # moved from, and the candidate path it lives at now.
    from_path: str | None = None
    to_path: str | None = None


class AdminSourceDiffManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
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
    renamed_count: int = 0
    # True when more files exist than the manifest bound returns; file_count
    # still reflects the real total so the omission is never silent.
    truncated: bool
    # Readable text files the bounded source read skipped in EITHER artifact
    # (combined text budget or file cap). They were NOT compared, so they appear
    # in no ``files`` row or count; ``file_count`` covers compared paths only.
    omitted_file_count: int = 0
    # The first MAX_OMITTED_PATHS omitted paths, sorted.
    omitted_paths: list[str] = Field(default_factory=list)


class AdminSourceDiffFileDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent_id: UUID
    reference_agent_id: UUID
    path: str
    candidate_present: bool
    reference_present: bool
    identical: bool
    diff_lines: list[str]
    truncated: bool
    from_path: str | None = None
    to_path: str | None = None


class AdminCopyReviewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # release/ban remain accepted for Backroom #20 wire compatibility.
    resolution: Literal["clear", "reject", "release", "ban"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]
    evidence_references: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(
                    strip_whitespace=True,
                    min_length=3,
                    max_length=512,
                    pattern=r"^\S+:(?:\d+(?:-\d+)?|[0-9a-fA-F-]{36})$",
                ),
            ]
        ],
        Field(min_length=1, max_length=64),
    ]
    reason_codes: Annotated[list[str], Field(max_length=16)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _validate_decision_evidence(self) -> "AdminCopyReviewResolveRequest":
        if self.resolution in ("clear", "release") and self.reason_codes:
            raise ValueError("clear must not assert a violation reason code")
        if self.resolution in ("reject", "ban"):
            if not self.reason_codes:
                raise ValueError(
                    "reject requires a published proven-violation reason code"
                )
            if any(
                not reference.rsplit(":", 1)[-1].split("-", 1)[0].isdigit()
                for reference in self.evidence_references
            ):
                raise ValueError("reject evidence must cite source path:line")
            unknown = unpublished_violation_codes(self.reason_codes)
            if unknown:
                raise ValueError(
                    f"unpublished proven-violation reason codes: {unknown}"
                )
        return self


class AdminCopyReviewOpenRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
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


class AdminCopyReviewPrecedent(BaseModel):
    """One decided ATH holding, compact enough to page as case law."""

    model_config = ConfigDict(extra="ignore")
    review_id: UUID
    agent_id: UUID
    agent_name: str
    agent_version: int | None = None
    miner_hotkey: str
    status: Literal["pending", "resolved"]
    resolution: Literal["clear", "reject"] | None = None
    resolution_reason: str | None = None
    original_reason: str | None = None
    review_kind: Literal["copy", "benchmark_overfit", "deferred_source_review"]
    opened_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class AdminCopyReviewPrecedentList(BaseModel):
    """Paged ATH holdings matched by reason text, identity, or resolution."""

    model_config = ConfigDict(extra="ignore")
    items: list[AdminCopyReviewPrecedent]
    count: int
    limit: int
    offset: int
    q: str | None = None
    resolution: Literal["clear", "reject", "all"]
    review_kind: (
        Literal["copy", "benchmark_overfit", "deferred_source_review"] | None
    ) = None
    status: Literal["resolved", "all"]

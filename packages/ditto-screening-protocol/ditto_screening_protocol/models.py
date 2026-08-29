"""Wire models for the Ditto screening boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

SCREENING_POLICY_VERSION = 11
# The oldest policy version a mixed-fleet platform may require during a
# scheduled activation window (v10 until v11 activates, then v11).
SCREENING_FLOOR_POLICY_VERSION = 10
TYPED_OUTCOME_POLICY_VERSION = 9

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"


class AgentStatus(StrEnum):
    """Lifecycle state of an agent submission."""

    UPLOADED = "uploaded"
    SCREENING = "screening"
    SCREENING_PASSED = "screening_passed"
    SCREENING_FAILED = "screening_failed"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    EVALUATING = "evaluating"
    SCORED = "scored"
    LIVE = "live"
    ATH_PENDING_REVIEW = "ath_pending_review"
    BANNED = "banned"


class ScreenResultOutcome(StrEnum):
    """Typed screener result; non-verdict outcomes never become rejection."""

    PASS = "pass"
    PASS_INCONCLUSIVE = "pass_inconclusive"
    DETERMINISTIC_REJECT = "deterministic_reject"
    RETRYABLE_INFRA = "retryable_infra"
    QUARANTINE = "quarantine"
    INCONCLUSIVE = "inconclusive"


class ArtifactResponse(BaseModel):
    """Short-lived artifact metadata returned to a screening worker."""

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    sha256: Annotated[
        str, Field(description="Expected SHA-256 of the tarball, lowercase hex.")
    ]
    download_url: Annotated[
        str, Field(description="Pre-signed URL used to download the tarball.")
    ]
    expires_at: Annotated[
        datetime, Field(description="When the download URL expires (UTC).")
    ]


SubmissionImageBuildStatus = Literal[
    "queued",
    "leased",
    "running",
    "succeeded",
    "fallback_required",
    "canceled",
    "consumed",
]


class SubmissionImageBuildRequest(BaseModel):
    """Queue one attempt-bound remote image build after local source validation."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID


class SubmissionImageBuildResponse(BaseModel):
    """Public-safe status and, when ready, the verified image archive."""

    model_config = ConfigDict(extra="ignore")

    build_id: UUID
    attempt_id: UUID
    status: SubmissionImageBuildStatus
    provider: Literal["targon", "gcp"] | None = None
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_ref: Annotated[
        str,
        Field(pattern=(r"^ditto-screen/[0-9a-f-]{73}:latest$")),
    ]
    output_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    output_size_bytes: Annotated[int, Field(gt=0, le=4 * 1024**3)] | None = None
    download_url: str | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None
    runtime_status: Literal[
        "pending", "running", "succeeded", "fallback_required", "skipped"
    ] = "skipped"
    runtime_provider: Literal["targon", "gcp"] | None = None
    runtime_image_reference: (
        Annotated[
            str,
            Field(
                pattern=r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
            ),
        ]
        | None
    ) = None
    runtime_error_code: (
        Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None
    ) = None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> SubmissionImageBuildResponse:
        output = (
            self.output_sha256,
            self.output_size_bytes,
            self.download_url,
        )
        if self.status == "succeeded" and any(value is None for value in output):
            raise ValueError("successful remote build requires a verified archive")
        if self.status != "succeeded" and any(value is not None for value in output):
            raise ValueError("only a successful remote build exposes an archive")
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("fallback remote build requires an error code")
        if self.runtime_status == "succeeded" and (
            self.runtime_provider not in ("targon", "gcp")
            or self.runtime_image_reference is None
        ):
            raise ValueError("successful runtime smoke requires provider provenance")
        if (
            self.runtime_status == "fallback_required"
            and self.runtime_error_code is None
        ):
            raise ValueError("runtime fallback requires an error code")
        return self


class ScreenedImageUploadRequest(BaseModel):
    """Lease-bound metadata used to mint a pre-signed image upload URL."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(gt=0, le=8 * 1024**3)]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    image_ref: Annotated[str, Field(pattern=r"^ditto-screen/[0-9a-f-]{36}:latest$")]


class ScreenedImageUploadResponse(BaseModel):
    """Lease-bound multipart upload initiated by the platform."""

    model_config = ConfigDict(extra="ignore")

    image_upload_id: UUID
    storage_upload_id: Annotated[str, Field(min_length=1, max_length=1024)]
    part_size_bytes: Annotated[int, Field(ge=5 * 1024**2, le=5 * 1024**3)]
    expires_at: datetime


class ScreenedImagePartUploadRequest(BaseModel):
    """Request a presigned URL for one part of an active image upload."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    storage_upload_id: Annotated[str, Field(min_length=1, max_length=1024)]
    part_number: Annotated[int, Field(ge=1, le=10_000)]
    size_bytes: Annotated[int, Field(gt=0, le=5 * 1024**3)]


class ScreenedImagePartUploadResponse(BaseModel):
    """Short-lived direct-to-object-storage URL for one multipart part."""

    model_config = ConfigDict(extra="ignore")

    upload_url: str
    expires_at: datetime
    required_headers: dict[str, str]


class ScreenedImageCompletedPart(BaseModel):
    """One uploaded multipart part and the storage ETag returned for it."""

    model_config = ConfigDict(extra="ignore")

    part_number: Annotated[int, Field(ge=1, le=10_000)]
    etag: Annotated[str, Field(min_length=1, max_length=256)]


class ScreenedImageUploadCompleteRequest(BaseModel):
    """Finalize a multipart image upload and request full-byte verification."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    storage_upload_id: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(gt=0, le=8 * 1024**3)]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    image_ref: Annotated[str, Field(pattern=r"^ditto-screen/[0-9a-f-]{36}:latest$")]
    parts: Annotated[
        list[ScreenedImageCompletedPart], Field(min_length=1, max_length=10_000)
    ]

    @model_validator(mode="after")
    def validate_part_sequence(self) -> ScreenedImageUploadCompleteRequest:
        if [part.part_number for part in self.parts] != list(
            range(1, len(self.parts) + 1)
        ):
            raise ValueError("completed image parts must be contiguous and ordered")
        return self


class ScreenedImageUploadCompleteResponse(BaseModel):
    """Acknowledgement that platform verification matched the signed archive."""

    model_config = ConfigDict(extra="ignore")

    verified: Literal[True]


class ScreenedImageUploadAbortRequest(BaseModel):
    """Abort an unfinished multipart upload owned by a screening attempt."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    storage_upload_id: Annotated[str, Field(min_length=1, max_length=1024)]


class ScreenedImageUploadAbortResponse(BaseModel):
    """Acknowledgement that an unfinished multipart upload was aborted."""

    model_config = ConfigDict(extra="ignore")

    aborted: bool


class ScreenerQueueItem(BaseModel):
    """One agent awaiting screening."""

    agent_id: Annotated[UUID, Field(description="Server-generated agent identifier.")]
    miner_hotkey: Annotated[str, Field(description="Submitting miner's SS58 hotkey.")]
    name: Annotated[str, Field(description="Miner-chosen agent name.")]
    sha256: Annotated[
        str, Field(description="SHA-256 of the uploaded tarball, lowercase hex.")
    ]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state at queue read time.")
    ]
    created_at: Annotated[
        datetime, Field(description="When the upload row was inserted (UTC).")
    ]
    attempt_id: Annotated[
        UUID | None,
        Field(
            description=(
                "Opaque lease id returned by the claim endpoint. Null only for "
                "legacy read-only queue responses."
            ),
        ),
    ] = None
    lease_deadline: Annotated[
        datetime | None,
        Field(
            description=(
                "UTC deadline for this screening attempt. A verdict arriving "
                "after it expires must not be accepted."
            ),
        ),
    ] = None
    precheck_reason_code: Annotated[
        str | None,
        Field(
            pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
            description=(
                "Platform-owned deterministic rejection discovered atomically "
                "while leasing. The worker must not download the artifact when set."
            ),
        ),
    ] = None
    duplicate_of: Annotated[
        UUID | None,
        Field(description="Earlier usable cross-miner submission for an exact copy."),
    ] = None
    build_only: Annotated[
        bool,
        Field(
            description=(
                "Selects the mechanical build/runtime lane. The platform uses it "
                "both for already-reviewed prerequisite rebuilds and for "
                "score-first admission. The screener skips deep source review but "
                "still performs every cheap fail-closed gate."
            ),
        ),
    ] = False
    policy_only: Annotated[
        bool,
        Field(
            description=(
                "Selects a policy-only rescreen of an artifact whose verified "
                "screened image and runtime smoke are already retained. The "
                "screener must reuse those mechanical results and rerun only "
                "the source/policy review."
            ),
        ),
    ] = False
    deferred_source_review: Annotated[
        bool,
        Field(
            description=(
                "True only for a fresh score-first admission whose deep source "
                "review is deferred. Unlike an already-reviewed rebuild, concrete "
                "mechanical or behavioral-oracle findings remain authoritative."
            )
        ),
    ] = False

    @model_validator(mode="after")
    def validate_precheck(self) -> ScreenerQueueItem:
        if (self.precheck_reason_code is None) != (self.duplicate_of is None):
            raise ValueError("precheck reason and duplicate reference must be paired")
        if self.deferred_source_review and not self.build_only:
            raise ValueError("deferred source review requires the mechanical lane")
        return self


class ScreenerQueueResponse(BaseModel):
    """Response returned by ``GET /screener/queue``."""

    items: Annotated[
        list[ScreenerQueueItem],
        Field(description="Agents awaiting screening, oldest first."),
    ]
    count: Annotated[int, Field(ge=0, description="Number of items returned.")]
    required_policy_version: Annotated[
        int,
        Field(
            ge=1,
            description="Minimum screening policy a passing verdict must attest.",
        ),
    ]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                        "miner_hotkey": (
                            "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
                        ),
                        "name": "alpha-agent",
                        "sha256": "deadbeef" * 8,
                        "status": "uploaded",
                        "created_at": "2026-06-08T12:00:00Z",
                    }
                ],
                "count": 1,
            }
        }
    )


class ScreenEvidenceItem(BaseModel):
    """One bounded, public-safe policy evidence summary carried on a verdict.

    Mirrors the screener's internal ``PolicyEvidence`` bounds. Raw challenge
    prompts, responses, private rules, credentials, and artifact source never
    belong here.
    """

    model_config = ConfigDict(extra="ignore")

    module_id: Annotated[
        str,
        Field(min_length=1, max_length=64, description="Reporting policy module."),
    ]
    code: Annotated[
        str,
        Field(min_length=1, max_length=64, description="Stable machine code."),
    ]
    summary: Annotated[
        str,
        Field(
            min_length=1,
            max_length=240,
            description="One bounded, public-safe sentence for the operator.",
        ),
    ]
    digest: Annotated[
        str | None,
        Field(
            pattern=r"^[0-9a-f]{64}$",
            description="Optional SHA-256 anchoring private evidence.",
        ),
    ] = None


class SourceReviewEvidenceItem(BaseModel):
    """One flagged source location from the read-only source review."""

    model_config = ConfigDict(extra="ignore")

    path: Annotated[str, Field(min_length=1, max_length=240)]
    line: Annotated[int, Field(ge=1)]
    category: Annotated[str, Field(min_length=1, max_length=64)]


class SourceReviewEvidenceRole(StrEnum):
    """Causal role proved by one artifact-bound source location."""

    SERVED_TRIGGER = "served_trigger"
    AUTHORITY_BYPASS = "authority_bypass"
    SCORER_VISIBLE_EFFECT = "scorer_visible_effect"
    REACHABILITY_LINK = "reachability_link"


class SourceReviewAuthorityTransition(StrEnum):
    """Bounded ways served code can make genuine model/tool output non-authoritative."""

    MODEL_SKIPPED = "model_skipped"
    MODEL_OUTPUT_OVERWRITTEN = "model_output_overwritten"
    TOOL_EXECUTION_BYPASSED = "tool_execution_bypassed"
    TOOL_TRAJECTORY_FABRICATED = "tool_trajectory_fabricated"
    SELECTIVE_MODEL_DISABLEMENT = "selective_model_disablement"
    SCORER_FIELD_REWRITTEN = "scorer_field_rewritten"
    DERIVED_VALUE_AUTHORITATIVE = "derived_value_authoritative"
    FAMILY_COMPILER_AUTHORITATIVE = "family_compiler_authoritative"
    TOOL_SELECTION_PREDETERMINED = "tool_selection_predetermined"


class SourceReviewScorerVisibleEffect(StrEnum):
    """Concrete graded field or validator-owned outcome changed by a transition."""

    FINAL_TEXT = "final_text"
    ANSWER = "answer"
    ABSTAIN = "abstain"
    TOOL_CALLS = "tool_calls"
    VALIDATOR_OBSERVED_TRAJECTORY = "validator_observed_trajectory"
    GRADED_OUTCOME = "graded_outcome"


_SCORER_VISIBLE_EFFECTS_BY_TRANSITION = {
    SourceReviewAuthorityTransition.MODEL_SKIPPED: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.MODEL_OUTPUT_OVERWRITTEN: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.TOOL_EXECUTION_BYPASSED: frozenset(
        {
            SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.TOOL_TRAJECTORY_FABRICATED: frozenset(
        {
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.SELECTIVE_MODEL_DISABLEMENT: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.SCORER_FIELD_REWRITTEN: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.DERIVED_VALUE_AUTHORITATIVE: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.FAMILY_COMPILER_AUTHORITATIVE: frozenset(
        {
            SourceReviewScorerVisibleEffect.FINAL_TEXT,
            SourceReviewScorerVisibleEffect.ANSWER,
            SourceReviewScorerVisibleEffect.ABSTAIN,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
    SourceReviewAuthorityTransition.TOOL_SELECTION_PREDETERMINED: frozenset(
        {
            SourceReviewScorerVisibleEffect.TOOL_CALLS,
            SourceReviewScorerVisibleEffect.VALIDATOR_OBSERVED_TRAJECTORY,
            SourceReviewScorerVisibleEffect.GRADED_OUTCOME,
        }
    ),
}


class SourceReviewInvariant(StrEnum):
    """Policy-v10 source-review invariants, evaluated independently."""

    MODEL_INVOCATION = "i1_model_invocation"
    EVIDENCE_RETENTION = "i2_evidence_retention"
    MODEL_DISSENT = "i3_model_dissent"
    DERIVED_VALUE_AUTHORITY = "i4_derived_value_authority"
    PRODUCTION_ENGINE = "i5_production_engine"
    TOOL_EXECUTION_FIDELITY = "i6_tool_execution_fidelity"
    MODEL_TOOL_PLANNING = "i7_model_tool_planning"


class SourceReviewInvariantDisposition(StrEnum):
    """One invariant's bounded conclusion."""

    PASS = "pass"
    BREACH = "breach"
    INCONCLUSIVE = "inconclusive"


class SourceReviewPassClause(StrEnum):
    """Published false-positive clauses that can refute a matching breach."""

    GENUINE_MODEL_RESULT = "genuine_model_result"
    NO_PREMODEL_RESPONSE = "no_premodel_response"
    FULL_RECORDS_ON_DECIDING_TURN = "full_records_on_deciding_turn"
    NON_AUTHORITATIVE_PRELIMINARY_PASS = "non_authoritative_preliminary_pass"
    SHAPE_ONLY_VALIDATION = "shape_only_validation"
    MODEL_DISSENT_PRESERVED = "model_dissent_preserved"
    NO_DERIVED_VALUE = "no_derived_value"
    UNTRUSTED_CANDIDATE_CHANNEL = "untrusted_candidate_channel"
    RUNTIME_DESCRIBED_GENERIC_ENGINE = "runtime_described_generic_engine"
    NO_FAMILY_COMPILER = "no_family_compiler"
    MODEL_SELECTED_EXECUTED_TOOL = "model_selected_executed_tool"
    NO_REPORTED_TOOL_CALLS = "no_reported_tool_calls"
    NO_TOOL_PLANNING = "no_tool_planning"
    POLICY_CAPABILITY_FILTER_ONLY = "policy_capability_filter_only"
    NATURAL_SINGLETON_CLASS = "natural_singleton_class"
    UNREACHABLE_NONRUNTIME_CODE = "unreachable_nonruntime_code"


_PASS_CLAUSES_BY_INVARIANT = {
    SourceReviewInvariant.MODEL_INVOCATION: frozenset(
        {
            SourceReviewPassClause.GENUINE_MODEL_RESULT,
            SourceReviewPassClause.NO_PREMODEL_RESPONSE,
        }
    ),
    SourceReviewInvariant.EVIDENCE_RETENTION: frozenset(
        {
            SourceReviewPassClause.FULL_RECORDS_ON_DECIDING_TURN,
            SourceReviewPassClause.NON_AUTHORITATIVE_PRELIMINARY_PASS,
        }
    ),
    SourceReviewInvariant.MODEL_DISSENT: frozenset(
        {
            SourceReviewPassClause.SHAPE_ONLY_VALIDATION,
            SourceReviewPassClause.MODEL_DISSENT_PRESERVED,
        }
    ),
    SourceReviewInvariant.DERIVED_VALUE_AUTHORITY: frozenset(
        {
            SourceReviewPassClause.NO_DERIVED_VALUE,
            SourceReviewPassClause.UNTRUSTED_CANDIDATE_CHANNEL,
        }
    ),
    SourceReviewInvariant.PRODUCTION_ENGINE: frozenset(
        {
            SourceReviewPassClause.RUNTIME_DESCRIBED_GENERIC_ENGINE,
            SourceReviewPassClause.NO_FAMILY_COMPILER,
        }
    ),
    SourceReviewInvariant.TOOL_EXECUTION_FIDELITY: frozenset(
        {
            SourceReviewPassClause.MODEL_SELECTED_EXECUTED_TOOL,
            SourceReviewPassClause.NO_REPORTED_TOOL_CALLS,
        }
    ),
    SourceReviewInvariant.MODEL_TOOL_PLANNING: frozenset(
        {
            SourceReviewPassClause.NO_TOOL_PLANNING,
            SourceReviewPassClause.POLICY_CAPABILITY_FILTER_ONLY,
            SourceReviewPassClause.NATURAL_SINGLETON_CLASS,
        }
    ),
}
for _invariant in SourceReviewInvariant:
    _PASS_CLAUSES_BY_INVARIANT[_invariant] = _PASS_CLAUSES_BY_INVARIANT[_invariant] | {
        SourceReviewPassClause.UNREACHABLE_NONRUNTIME_CODE
    }


class SourceReviewInvariantDecision(BaseModel):
    """One policy-v10 invariant decision and its false-positive valve."""

    model_config = ConfigDict(extra="ignore")

    invariant: SourceReviewInvariant
    disposition: SourceReviewInvariantDisposition
    pass_clause: SourceReviewPassClause | None = None
    summary: Annotated[str, Field(min_length=1, max_length=240)]
    evidence_indices: Annotated[
        list[Annotated[int, Field(ge=0, le=15)]],
        Field(default_factory=list, max_length=16),
    ]

    @model_validator(mode="after")
    def validate_pass_clause(self) -> Self:
        if self.disposition == SourceReviewInvariantDisposition.PASS:
            if self.pass_clause not in _PASS_CLAUSES_BY_INVARIANT[self.invariant]:
                raise ValueError("invariant pass clause is missing or incompatible")
            if self.evidence_indices:
                raise ValueError("passing invariant cannot carry violation evidence")
        elif self.pass_clause is not None:
            raise ValueError("only a passing invariant may name a pass clause")
        if (
            self.disposition == SourceReviewInvariantDisposition.BREACH
            and not self.evidence_indices
        ):
            raise ValueError("invariant breach requires source evidence")
        if len(self.evidence_indices) != len(set(self.evidence_indices)):
            raise ValueError("invariant evidence indices must be unique")
        return self


class SourceReviewInvariantAssessment(BaseModel):
    """Complete policy-v10 sweep; omission cannot silently clear an invariant."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1] = 1
    decisions: Annotated[
        list[SourceReviewInvariantDecision], Field(min_length=7, max_length=7)
    ]

    @model_validator(mode="after")
    def validate_complete_sweep(self) -> Self:
        invariants = [decision.invariant for decision in self.decisions]
        if len(invariants) != len(set(invariants)):
            raise ValueError("invariant decisions must be unique")
        if set(invariants) != set(SourceReviewInvariant):
            raise ValueError("source review must decide every policy-v10 invariant")
        return self


class SourceReviewCausalRoleBinding(BaseModel):
    """One role assigned to an existing public-safe finding location."""

    model_config = ConfigDict(extra="ignore")

    path: Annotated[str, Field(min_length=1, max_length=240)]
    line: Annotated[int, Field(ge=1)]
    category: Annotated[str, Field(min_length=1, max_length=64)]
    role: SourceReviewEvidenceRole


class SourceReviewCausalEvidence(BaseModel):
    """Opt-in v2 causal evidence carried alongside the legacy location list."""

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[2] = 2
    authority_transition: SourceReviewAuthorityTransition
    scorer_visible_effect: SourceReviewScorerVisibleEffect
    role_bindings: Annotated[
        list[SourceReviewCausalRoleBinding], Field(min_length=1, max_length=32)
    ]

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> Self:
        keys = [
            (item.path, item.line, item.category, item.role)
            for item in self.role_bindings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("causal role bindings must be unique")
        if (
            self.scorer_visible_effect
            not in _SCORER_VISIBLE_EFFECTS_BY_TRANSITION[self.authority_transition]
        ):
            raise ValueError(
                "scorer-visible effect is incompatible with authority transition"
            )
        return self


_ROLE_COMPLETE_CATEGORIES = frozenset(
    {"benchmark_emulation", "scorer_contract_manipulation"}
)
_REQUIRED_CAUSAL_ROLES = frozenset(SourceReviewEvidenceRole)


class SourceReviewFinding(BaseModel):
    """Bounded source-review finding whose canonical JSON is digest-bound.

    ``canonical_digest()`` over this payload must equal the ``finding_digest``
    bound into the signed verdict, letting the platform verify the finding it
    stores is exactly the one the screener attested.
    """

    model_config = ConfigDict(extra="ignore")

    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    prompt_revision: Annotated[str, Field(min_length=1, max_length=64)]
    risk_level: Literal["low", "medium", "high"]
    confidence: Annotated[float, Field(ge=0, le=1)]
    categories: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Field(min_length=1, max_length=8),
    ]
    evidence: Annotated[
        list[SourceReviewEvidenceItem], Field(default_factory=list, max_length=16)
    ]
    summary: Annotated[str, Field(min_length=1, max_length=240)]
    causal_evidence: SourceReviewCausalEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional v2 role bindings. Absence is the historical v1 schema and "
            "retains its exact canonical payload."
        ),
    )
    invariant_assessment: SourceReviewInvariantAssessment | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Optional complete policy-v10 invariant sweep. Historical findings "
            "remain byte-identical when absent."
        ),
    )

    @model_validator(mode="after")
    def validate_causal_binding_locations(self) -> Self:
        categories = set(self.categories)
        evidence_locations = {
            (item.path, item.line, item.category) for item in self.evidence
        }
        if self.causal_evidence is not None:
            for binding in self.causal_evidence.role_bindings:
                if binding.category not in categories:
                    raise ValueError("causal role binding category is not in finding")
                if (
                    binding.path,
                    binding.line,
                    binding.category,
                ) not in evidence_locations:
                    raise ValueError(
                        "causal role binding does not reference finding evidence"
                    )
        if self.invariant_assessment is not None:
            for decision in self.invariant_assessment.decisions:
                if any(
                    index >= len(self.evidence) for index in decision.evidence_indices
                ):
                    raise ValueError(
                        "invariant decision evidence index is out of range"
                    )
        return self

    @property
    def evidence_schema_version(self) -> Literal[1, 2, 3]:
        """Return the effective evidence schema without changing v1 wire JSON."""
        if self.invariant_assessment is not None:
            return 3
        return 2 if self.causal_evidence is not None else 1

    def require_policy_v10_invariants(self) -> Self:
        """Require a complete, self-consistent strict invariant sweep."""

        if self.invariant_assessment is None:
            raise ValueError("policy v10 finding requires invariant assessment")
        dispositions = {
            decision.disposition for decision in self.invariant_assessment.decisions
        }
        elevated = bool(
            dispositions
            & {
                SourceReviewInvariantDisposition.BREACH,
                SourceReviewInvariantDisposition.INCONCLUSIVE,
            }
        )
        categories = set(self.categories)
        strict_categories = categories & {
            "benchmark_emulation",
            "scorer_contract_manipulation",
            "fabricated_tool_trajectory",
        }
        if self.risk_level == "low" and elevated:
            raise ValueError("low-risk finding has unresolved policy-v10 invariant")
        if strict_categories and not elevated:
            raise ValueError("strict source category lacks a policy-v10 breach")
        if "none" in categories and elevated:
            raise ValueError("none category cannot carry an elevated invariant")
        return self

    def require_role_complete_causal_evidence(self) -> Self:
        """Require v2 role completeness for elevated causal categories.

        Parsing remains backward compatible: callers opt into this stricter
        policy check when a reviewer revision is ready to enforce v2. This
        method deliberately does not invalidate historical v1 findings merely
        because they predate causal role bindings.
        """
        required_categories = set(self.categories) & _ROLE_COMPLETE_CATEGORIES
        if not required_categories:
            return self
        if self.causal_evidence is None:
            raise ValueError("finding requires causal evidence schema v2")
        bindings_by_category: dict[str, list[SourceReviewCausalRoleBinding]] = {}
        for binding in self.causal_evidence.role_bindings:
            bindings_by_category.setdefault(binding.category, []).append(binding)
        for category in sorted(required_categories):
            bindings = bindings_by_category.get(category, [])
            roles = {item.role for item in bindings}
            missing = _REQUIRED_CAUSAL_ROLES - roles
            if missing:
                missing_values = ", ".join(sorted(role.value for role in missing))
                raise ValueError(
                    f"source review category {category} is missing causal roles: "
                    f"{missing_values}"
                )
            locations = {(item.path, item.line) for item in bindings}
            if len(locations) < 2:
                raise ValueError(
                    f"source review category {category} requires two causal locations"
                )
        return self

    def canonical_bytes(self) -> bytes:
        """Return the exact versioned canonical JSON bytes for signing."""
        payload: dict[str, object] = {
            "artifact_sha256": self.artifact_sha256,
            "prompt_revision": self.prompt_revision,
            "risk_level": self.risk_level,
            "confidence": self.confidence,
            "categories": sorted(set(self.categories)),
            "evidence": [
                {
                    "path": item.path,
                    "line": item.line,
                    "category": item.category,
                }
                for item in self.evidence
            ],
            "summary": self.summary,
        }
        if self.causal_evidence is not None:
            payload["causal_evidence"] = {
                "schema_version": self.causal_evidence.schema_version,
                "authority_transition": (
                    self.causal_evidence.authority_transition.value
                ),
                "scorer_visible_effect": (
                    self.causal_evidence.scorer_visible_effect.value
                ),
                "role_bindings": [
                    {
                        "path": item.path,
                        "line": item.line,
                        "category": item.category,
                        "role": item.role.value,
                    }
                    for item in sorted(
                        self.causal_evidence.role_bindings,
                        key=lambda binding: (
                            binding.role.value,
                            binding.path,
                            binding.line,
                            binding.category,
                        ),
                    )
                ],
            }
        if self.invariant_assessment is not None:
            payload["invariant_assessment"] = {
                "schema_version": self.invariant_assessment.schema_version,
                "decisions": [
                    {
                        "invariant": decision.invariant.value,
                        "disposition": decision.disposition.value,
                        **(
                            {"pass_clause": decision.pass_clause.value}
                            if decision.pass_clause is not None
                            else {}
                        ),
                        "summary": decision.summary,
                        "evidence_indices": sorted(decision.evidence_indices),
                    }
                    for decision in sorted(
                        self.invariant_assessment.decisions,
                        key=lambda decision: decision.invariant.value,
                    )
                ],
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def canonical_digest(self) -> str:
        """SHA-256 over the canonical JSON encoding of this finding."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ScreenReviewAudit(BaseModel):
    """Public-safe accounting for a bounded review that could not conclude."""

    model_config = ConfigDict(extra="ignore")

    stage: Literal["l1", "l2"]
    reason_code: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    prompt_revision: Annotated[str, Field(min_length=1, max_length=64)]
    harness_revision: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    max_steps: Annotated[int, Field(ge=1, le=100)]
    steps_used: Annotated[int, Field(ge=0, le=100)]
    max_read_bytes: Annotated[int | None, Field(ge=1, le=256 * 1024**2)] = None
    read_bytes_used: Annotated[int | None, Field(ge=0, le=256 * 1024**2)] = None
    max_input_tokens: Annotated[int | None, Field(ge=1, le=2_000_000)] = None
    input_tokens_used: Annotated[int | None, Field(ge=0, le=2_000_000)] = None
    max_output_tokens: Annotated[int | None, Field(ge=1, le=256_000)] = None
    output_tokens_used: Annotated[int | None, Field(ge=0, le=256_000)] = None
    max_cost_usd: Annotated[float | None, Field(gt=0, le=100)] = None
    cost_usd_used: Annotated[float | None, Field(ge=0, le=100)] = None

    @model_validator(mode="after")
    def validate_pairs_and_usage(self) -> ScreenReviewAudit:
        for maximum, used, label in (
            (self.max_read_bytes, self.read_bytes_used, "read bytes"),
            (self.max_input_tokens, self.input_tokens_used, "input tokens"),
            (self.max_output_tokens, self.output_tokens_used, "output tokens"),
            (self.max_cost_usd, self.cost_usd_used, "cost"),
        ):
            if (maximum is None) != (used is None):
                raise ValueError(f"{label} maximum and usage must be paired")
        if self.steps_used > self.max_steps:
            raise ValueError("review steps used exceed configured maximum")
        return self

    def canonical_digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


SubmissionSourceReviewStatus = Literal[
    "queued",
    "leased",
    "running",
    "succeeded",
    "fallback_required",
    "canceled",
    "consumed",
]


class SubmissionSourceReviewRequest(BaseModel):
    """Queue one attempt-bound remote, read-only source review."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID


class SourceReviewNote(BaseModel):
    """One bounded, typed determination recorded DURING a source review.

    The review agent is required to log its working determinations as it
    inspects, so a budget- or fault-terminated review still yields the
    evidence it accumulated instead of a bare ``inconclusive``. ``concern``
    notes accumulate toward an operator hold; ``cleared`` notes accumulate
    toward positive coverage; ``observation`` is neutral context. Summaries
    are reviewer-authored and public-safe: never source text, prompts, or
    challenge values.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["concern", "cleared", "observation"]
    category: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")] = "none"
    path: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    line: Annotated[int, Field(ge=1, le=10_000_000)] | None = None
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    stage: Literal["l1", "l2", "l3"] = "l1"


class AdjudicationClearClause(StrEnum):
    """Published court false positives an automated clear may cite.

    These are the release-with-a-refutation classes the operator review rules
    already recognise. Making the vocabulary closed is the point: an
    adjudicator that has to name one of these cannot free-associate its way to
    a clear, and an operator reading the audit trail can check the claim
    against the same published list.
    """

    RETRIEVAL_RANKING = "retrieval_ranking_not_family_engine"
    MEMOIZATION_CACHE = "content_complete_memoization_cache"
    STANDARD_BROKER_CLIENT = "standard_broker_inference_client"
    UNREPORTED_EXECUTED_CALLS = "unreported_tool_calls_executed"
    PRACTICE_HARNESS_STUB = "local_practice_harness_stub"
    INTENT_ROUTING = "intent_routing_or_precursor_pass"
    BENCH_VERSION_BRANCHING = "bench_version_branching_alone"
    DUPLICATE_SUPPRESSION = "single_success_duplicate_suppression"
    ANSWER_NORMALIZATION = "plain_answer_normalization"
    PRIOR_PATTERN_REMOVED = "prior_pattern_removed"
    MODEL_AUTHORS_GRADED_SLOT = "model_authors_graded_slot"


class SourceReviewCitation(BaseModel):
    """One ``path:line`` the adjudicator read and is relying on."""

    model_config = ConfigDict(extra="ignore")

    path: Annotated[str, Field(min_length=1, max_length=240)]
    line: Annotated[int, Field(ge=1, le=10_000_000)]


class SourceReviewAdjudication(BaseModel):
    """Terminal clear/reject decision on a review that would otherwise hold.

    ``escalate`` is never a model choice. The adjudicator is asked for clear
    or reject; the host substitutes ``escalate`` when the returned decision
    fails its contract (an uncited verdict, a citation the adjudicator never
    read, a hallucinated path, a missing published basis). The hold then
    stands and an operator sees it, so a malformed adjudication can only cost
    latency -- never a wrong release or a wrong ban.
    """

    model_config = ConfigDict(extra="ignore")

    decision: Literal["clear", "reject", "escalate"]
    reason: Annotated[str, Field(min_length=1)]
    """Miner-visible. Deliberately unbounded at the wire: operator reason
    fields carry audit evidence and a schema cap silently truncates it, which
    ``test_operator_reason_fields_have_no_upper_bound`` enforces repo-wide.
    The adjudicator bounds its own reason where the untrusted model output is
    first parsed instead."""
    reject_invariant: SourceReviewInvariant | None = None
    clear_clause: AdjudicationClearClause | None = None
    citations: Annotated[
        list[SourceReviewCitation], Field(default_factory=list, max_length=8)
    ]
    notes_considered: Annotated[int, Field(ge=0, le=48)] = 0
    model: Annotated[str, Field(min_length=1, max_length=120)]
    prompt_revision: Annotated[str, Field(min_length=1, max_length=80)]
    policy_version: Annotated[int, Field(ge=1, le=1_000)] = SCREENING_POLICY_VERSION
    escalation_code: (
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] | None
    ) = None

    @model_validator(mode="after")
    def validate_decision_basis(self) -> SourceReviewAdjudication:
        if self.decision == "reject":
            if self.reject_invariant is None:
                raise ValueError("a reject must name the policy invariant it breached")
            if self.clear_clause is not None:
                raise ValueError("a reject cannot cite a false-positive clause")
            if not self.citations:
                raise ValueError("a reject requires at least one cited location")
        elif self.decision == "clear":
            if self.clear_clause is None:
                raise ValueError("a clear must name the published clause it relies on")
            if self.reject_invariant is not None:
                raise ValueError("a clear cannot name a breached invariant")
            if not self.citations:
                raise ValueError("a clear requires at least one cited location")
        elif self.escalation_code is None:
            raise ValueError("an escalation must name why the decision was refused")
        return self

    def canonical_digest(self) -> str:
        """Bind the complete court result into the signed worker verdict."""
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class SourceReviewObservationPayload(BaseModel):
    """Bounded source-review observation safe to cross provider boundaries."""

    model_config = ConfigDict(extra="ignore")

    ok: bool
    risk_level: Literal["low", "medium", "high"] | None = None
    finding_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    categories: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Field(default_factory=list, max_length=8),
    ]
    error_code: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] | None = (
        None
    )
    finding: SourceReviewFinding | None = None
    failure_disposition: Literal[
        "retryable_infra", "inconclusive", "pass_inconclusive"
    ] = "retryable_infra"
    clearance_certified: bool = False
    review_audit: ScreenReviewAudit | None = None
    notes: Annotated[list[SourceReviewNote], Field(default_factory=list, max_length=48)]
    adjudication: SourceReviewAdjudication | None = None
    """Automated clear/reject on a review that would otherwise hold. Absent
    when the adjudicator is off, when the review needed no adjudication, or
    when the adjudicator itself failed."""

    @model_validator(mode="after")
    def validate_finding_binding(self) -> SourceReviewObservationPayload:
        if self.finding is not None:
            if self.finding_digest is None:
                raise ValueError("source-review finding requires its digest")
            if self.finding.canonical_digest() != self.finding_digest:
                raise ValueError("source-review finding does not match its digest")
        if self.ok and self.risk_level is None:
            raise ValueError("successful source review requires a risk level")
        return self


class SubmissionSourceReviewResponse(BaseModel):
    """Status and terminal observation for an attempt-bound remote review."""

    model_config = ConfigDict(extra="ignore")

    review_id: UUID
    attempt_id: UUID
    status: SubmissionSourceReviewStatus
    provider: Literal["targon", "gcp"] | None = None
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    observation: SourceReviewObservationPayload | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> SubmissionSourceReviewResponse:
        if self.status == "succeeded" and self.observation is None:
            raise ValueError("successful remote source review requires an observation")
        if self.status != "succeeded" and self.observation is not None:
            raise ValueError("only a successful remote source review exposes a result")
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("fallback source review requires an error code")
        return self


class ScreenResultRequest(BaseModel):
    """Signed result posted to ``/screener/agent/{agent_id}/result``."""

    screener_hotkey: Annotated[
        str,
        Field(pattern=_SS58_PATTERN, description="Reporting screener's SS58 hotkey."),
    ]
    attempt_id: Annotated[
        UUID | None,
        Field(
            description=(
                "Claimed screening-attempt lease. Required by lease-aware "
                "platforms and bound into the v2 verdict signature."
            ),
        ),
    ] = None
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description="Hex sr25519 signature over the versioned verdict.",
        ),
    ]
    passed: Annotated[
        bool,
        Field(description="True promotes to evaluating; False -> screening_failed."),
    ]
    outcome: ScreenResultOutcome | None = None
    manifest_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    finding_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    review_audit_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    adjudication_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = (
        None
    )
    review_settings_revision: Annotated[int | None, Field(ge=1)] = None
    review_settings_instance_id: Annotated[
        str | None, Field(pattern=r"^[a-zA-Z0-9._-]{1,63}$")
    ] = None
    review_settings_scope: Annotated[
        str | None, Field(pattern=r"^(?:\*|[a-zA-Z0-9._-]{1,63})$")
    ] = None
    review_settings_checksum: Annotated[
        str | None, Field(pattern=r"^[0-9a-f]{64}$")
    ] = None
    reason_code: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] = (
        None
    )
    image_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    image_size_bytes: Annotated[int | None, Field(gt=0, le=8 * 1024**3)] = None
    image_id: Annotated[str | None, Field(pattern=r"^sha256:[0-9a-f]{64}$")] = None
    image_ref: Annotated[
        str | None, Field(pattern=r"^ditto-screen/[0-9a-f-]{36}:latest$")
    ] = None
    image_upload_id: UUID | None = None
    evidence: Annotated[
        list[ScreenEvidenceItem] | None,
        Field(
            max_length=16,
            description=(
                "Bounded public-safe policy evidence trail for operator review. "
                "Carried over the authenticated screener channel; the platform "
                "must treat it as display data, not proof."
            ),
        ),
    ] = None
    finding: Annotated[
        SourceReviewFinding | None,
        Field(
            description=(
                "Bounded source-review finding. Its canonical digest must equal "
                "finding_digest, which is bound into the verdict signature."
            ),
        ),
    ] = None
    review_audit: Annotated[
        ScreenReviewAudit | None,
        Field(
            description=(
                "Public-safe, digest-bound budget accounting for a terminal "
                "pass-inconclusive review."
            )
        ),
    ] = None
    adjudication: SourceReviewAdjudication | None = None
    policy_version: Annotated[
        int,
        Field(
            default=1,
            ge=1,
            description="Screening policy version bound into the signature.",
        ),
    ]
    detail: Annotated[
        str,
        Field(
            default="",
            max_length=4000,
            description=(
                "Optional reason / build-log tail; the platform must treat it as "
                "untrusted."
            ),
        ),
    ]
    build_only: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Echoes the claimed item's build-only mode: this verdict came "
                "from a mechanical build-only pass that skipped anti-cheat "
                "review. A build-only verdict can never carry a quarantine "
                "outcome. Unsigned display/context only; the platform must not "
                "treat it as proof."
            ),
        ),
    ]
    deferred_source_review: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Signed echo of a platform-issued score-first mechanical claim. "
                "The platform must verify it against the immutable attempt marker."
            ),
        ),
    ] = False
    policy_only: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Signed echo of a platform-issued policy-only rescreen. A "
                "passing result reuses the agent's retained verified image."
            ),
        ),
    ] = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "screener_hotkey": ("5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"),
                "signature": "ab" * 64,
                "passed": True,
                "policy_version": SCREENING_POLICY_VERSION,
                "detail": "",
            }
        }
    )

    @model_validator(mode="after")
    def validate_typed_outcome(self) -> ScreenResultRequest:
        if self.outcome is None:
            if self.policy_version >= TYPED_OUTCOME_POLICY_VERSION:
                raise ValueError("policy v9+ result requires typed outcome")
            if any(
                value is not None
                for value in (
                    self.image_sha256,
                    self.image_size_bytes,
                    self.image_id,
                    self.image_ref,
                    self.image_upload_id,
                )
            ):
                raise ValueError("legacy result cannot carry screened image metadata")
            return self
        if self.passed != (
            self.outcome
            in {ScreenResultOutcome.PASS, ScreenResultOutcome.PASS_INCONCLUSIVE}
        ):
            raise ValueError("passed must agree with outcome")
        if (
            self.outcome
            in {
                ScreenResultOutcome.QUARANTINE,
                ScreenResultOutcome.INCONCLUSIVE,
                ScreenResultOutcome.PASS_INCONCLUSIVE,
            }
            and self.attempt_id is None
        ):
            raise ValueError("review outcome requires attempt_id")
        if self.outcome in {
            ScreenResultOutcome.QUARANTINE,
            ScreenResultOutcome.PASS_INCONCLUSIVE,
        } and (self.manifest_digest is None or self.reason_code is None):
            raise ValueError("review result requires manifest_digest and reason_code")
        image_fields = (
            self.image_sha256,
            self.image_size_bytes,
            self.image_id,
            self.image_ref,
            self.image_upload_id,
        )
        if self.outcome in {
            ScreenResultOutcome.PASS,
            ScreenResultOutcome.PASS_INCONCLUSIVE,
        }:
            if not self.policy_only and any(value is None for value in image_fields):
                raise ValueError("passing policy-v9 result requires screened image")
            if self.policy_only and any(value is not None for value in image_fields):
                raise ValueError("policy-only result must reuse the retained image")
        elif any(value is not None for value in image_fields):
            raise ValueError("screened image metadata requires passing outcome")
        return self

    @model_validator(mode="after")
    def validate_review_payloads(self) -> ScreenResultRequest:
        settings_binding = (
            self.review_settings_revision,
            self.review_settings_instance_id,
            self.review_settings_scope,
            self.review_settings_checksum,
        )
        if any(value is not None for value in settings_binding) and any(
            value is None for value in settings_binding
        ):
            raise ValueError("review settings binding must be complete")
        if (self.evidence is not None or self.finding is not None) and (
            self.outcome
            not in {
                ScreenResultOutcome.QUARANTINE,
                ScreenResultOutcome.INCONCLUSIVE,
                ScreenResultOutcome.PASS_INCONCLUSIVE,
            }
        ):
            raise ValueError("evidence and finding require a review outcome")
        if self.finding is not None:
            if self.finding_digest is None:
                raise ValueError("finding requires finding_digest")
            if self.finding.canonical_digest() != self.finding_digest:
                raise ValueError("finding does not match finding_digest")
        if self.outcome == ScreenResultOutcome.PASS_INCONCLUSIVE:
            if self.review_audit is None or self.review_audit_digest is None:
                raise ValueError("pass-inconclusive requires review audit")
            if self.review_audit.canonical_digest() != self.review_audit_digest:
                raise ValueError("review audit does not match review_audit_digest")
        elif self.review_audit is not None or self.review_audit_digest is not None:
            raise ValueError("review audit requires pass-inconclusive outcome")
        if (self.adjudication is None) != (self.adjudication_digest is None):
            raise ValueError(
                "adjudication and adjudication_digest must travel together"
            )
        if self.adjudication is not None:
            if self.review_settings_revision is None:
                raise ValueError("adjudication requires reviewer settings binding")
            if self.adjudication.canonical_digest() != self.adjudication_digest:
                raise ValueError("adjudication does not match adjudication_digest")
            if self.outcome not in {
                ScreenResultOutcome.PASS,
                ScreenResultOutcome.QUARANTINE,
            }:
                raise ValueError("adjudication requires a pass or quarantine outcome")
            if (
                self.adjudication.decision == "reject"
                and self.outcome != ScreenResultOutcome.QUARANTINE
            ):
                raise ValueError("adjudicated reject requires quarantine transport")
        return self

    @model_validator(mode="after")
    def validate_build_only(self) -> ScreenResultRequest:
        if self.deferred_source_review and not self.build_only:
            raise ValueError("deferred source review requires the mechanical lane")
        # A historical prerequisite rebuild has already been adjudicated and
        # cannot create a new quarantine. A fresh deferred admission, however,
        # must keep concrete cheap behavioral/oracle findings fail-closed.
        if (
            self.build_only
            and not self.deferred_source_review
            and self.outcome == ScreenResultOutcome.QUARANTINE
        ):
            raise ValueError("build-only result cannot carry a quarantine outcome")
        return self


class ScreenResultResponse(BaseModel):
    """Response returned after a screener verdict is applied."""

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state after the verdict.")
    ]
    accepted: Annotated[bool, Field(description="True when the verdict was applied.")]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "evaluating",
                "accepted": True,
            }
        }
    )

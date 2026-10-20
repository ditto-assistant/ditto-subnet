"""Operator-only per-case v13 claim-provenance read (issue #1852).

The public score record carries the run-level claim-provenance aggregate only
("4 served_text_not_model_emitted out of 153 settled"), and the owner-only
gate notes carry the finding names. Neither lets an operator see *why* one
exact case was flagged before ruling on an exact artifact. This read projects
the per-case record the Platform already persists in ``scores.details``, keyed
by the exact agent, artifact SHA-256, accepted run and (optionally) case.

It is hash-derived verdicts, counts and digests only. The scorer's answer key
(``expected``), prompts, user records, tool results and completion text are
never part of it. What the scorer computes but does not persist is named in
``not_persisted`` so an absent field is never read as absent evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ditto.api_models.gate_evidence import (
    GATE_EVIDENCE_MIN_BENCH_VERSION,
    ClaimProvenanceSummary,
    GatePosture,
)

CLAIM_PROVENANCE_CASE_LIMIT_MAX = 100
"""Most cases one read returns; a v13 run has a few hundred."""

CATALOG_COMPLETION_LIMIT = 32
"""Most per-completion relay records shown per case."""

SCORER_NOTE_LIMIT = 8
SCORER_NOTE_MAX_CHARS = 400

WITHHELD_SCORER_NOTE = "withheld: this scorer note quotes a case value"
"""Replaces any stored scorer note that quotes a value.

The Go scorers interpolate case values into notes with ``%q`` -- the v13 tool
scorer's forbidden argument value, the injection bait tool name, the case
language, and the memory grader's distractor value -- so a note carrying a
double quote is withheld rather than forwarded, even to operators.
"""

NotPersistedField = Literal[
    "credited_response_field",
    "claim_token_comparison",
    "attributed_completion_ids",
    "normalization_explanation",
]

NOT_PERSISTED_FIELDS: tuple[NotPersistedField, ...] = (
    "credited_response_field",
    "claim_token_comparison",
    "attributed_completion_ids",
    "normalization_explanation",
)

NOT_PERSISTED_REASON = (
    "The scorer computes the credited span/field and the per-token claim "
    "comparison while grading, but its ClaimProvenanceEvidence wire record "
    "carries only verdicts and counts, the claim-span ledger keeps no "
    "per-completion identifiers, and no normalization trace is recorded. "
    "Showing them needs a scorer and wire change; their absence here is not "
    "evidence either way."
)


class CaseGateNote(BaseModel):
    """One closed-vocabulary finding on the case, with its dispute id."""

    gate: str
    zeroing: Annotated[
        bool,
        Field(description="Whether this finding zeroes the case under enforce."),
    ]
    note_id: Annotated[
        str,
        Field(
            description=(
                "The id an owner dispute cites for this note "
                "(same derivation as the miner gate-notes read)."
            )
        ),
    ]


class CaseClaimProvenance(BaseModel):
    """The persisted per-case ``claim_provenance`` record."""

    posture: str
    findings: list[str] = Field(default_factory=list)
    completions: Annotated[
        int | None,
        Field(description="Model completions attributed to the case; null if unknown."),
    ] = None
    unattributed_calls: int = 0
    tool_results: int = 0
    claim_tokens: Annotated[
        int,
        Field(description="Tokens in the credited claim span (count only)."),
    ] = 0
    complete: Annotated[
        bool,
        Field(description="Whether the case's completion attribution was complete."),
    ] = False
    model_emitted: Annotated[
        bool | None,
        Field(
            description="Claim tokens found in a model completion; null if unsettled."
        ),
    ] = None
    answer_in_prompt: Annotated[
        bool | None,
        Field(description="Claim tokens already present in harness-sent input."),
    ] = None


class CaseCatalogCompletion(BaseModel):
    """Relay metadata for one attributed completion (digests, no text)."""

    attribution_source: str = ""
    claim_corroborated: bool = False
    after_last_tool_result: bool = False
    tool_choice: str = ""
    tools_offered: int = 0
    tools_choosable: int = 0
    model_emitted_tool_calls: list[str] = Field(default_factory=list)
    catalog_sha256: str = ""
    system_span_sha256: str = ""


class CaseCatalog(BaseModel):
    """The persisted per-case ``catalog`` record, bounded."""

    catalog_present: bool = False
    catalog_present_lower_bound: bool = False
    tools_offered: int = 0
    completions_total: int | None = None
    completions_with_catalog: int = 0
    claim_attributed_completions: int = 0
    claim_corroborated_completions: int = 0
    complete: bool = False
    findings: list[str] = Field(default_factory=list)
    completions: list[CaseCatalogCompletion] = Field(
        default_factory=list, max_length=CATALOG_COMPLETION_LIMIT
    )
    completions_truncated: bool = False


class ClaimProvenanceCase(BaseModel):
    """One case's persisted gate evidence, as an operator reads it."""

    case_index: Annotated[int, Field(ge=0)]
    case_id: str
    category: str
    kind: str
    score: float
    correct: bool
    gate_notes: list[CaseGateNote] = Field(default_factory=list)
    claim_provenance: CaseClaimProvenance | None = None
    catalog: CaseCatalog | None = None
    relation: str | None = None
    twin_group: str | None = None
    cost_factor: Annotated[
        float | None,
        Field(description="Shadow inference-cost factor (1.0 = no discount)."),
    ] = None
    scorer_notes: Annotated[
        list[str],
        Field(
            description=(
                "The scorer's own per-case notes, bounded. A note that quotes a "
                "case value (forbidden argument, bait tool, distractor) is "
                "replaced by a fixed withheld marker."
            ),
            max_length=SCORER_NOTE_LIMIT,
        ),
    ] = Field(default_factory=list)


class AdminClaimProvenanceCases(BaseModel):
    """Per-case claim provenance for one exact agent, artifact and accepted run."""

    agent_id: UUID
    artifact_sha256: str
    agent_status: str
    validator_hotkey: str
    run_id: str
    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    composite: float
    generated_at: datetime
    posture: GatePosture | None = None
    claim_provenance: Annotated[
        ClaimProvenanceSummary | None,
        Field(description="The run-level aggregate this per-case view explains."),
    ] = None
    case_id: str | None = None
    finding: str | None = None
    include_unflagged: bool = False
    per_case_available: Annotated[
        bool,
        Field(description="False when the accepted row stored no per-case breakdown."),
    ]
    total_cases: Annotated[int, Field(ge=0)]
    matched_cases: Annotated[int, Field(ge=0)]
    malformed_cases: Annotated[
        int,
        Field(ge=0, description="Stored cases that no longer parse; skipped."),
    ] = 0
    limit: Annotated[int, Field(ge=1, le=CLAIM_PROVENANCE_CASE_LIMIT_MAX)]
    truncated: bool
    cases: list[ClaimProvenanceCase] = Field(default_factory=list)
    not_persisted: list[NotPersistedField] = Field(
        default_factory=lambda: list(NOT_PERSISTED_FIELDS)
    )
    not_persisted_reason: str = NOT_PERSISTED_REASON

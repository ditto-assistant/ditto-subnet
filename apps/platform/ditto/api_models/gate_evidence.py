"""Bench v13 gate evidence: per-case gate notes and shadow verdicts.

Bench v13 adds score gates that can zero a case (catalog-present, claim-span
provenance, causal ``answer_in_prompt``, the twin / counterfactual pair rule)
and a shadow cost factor. Every one of them ships in **shadow** first: the
scorer computes what the gate *would* have done and reports it beside the
unchanged score. A gate a miner cannot see is a surprise zero at enforce with
no evidence to appeal, so the scorer's per-case findings and run-level
summaries are persisted on the accepted score and exposed on three surfaces
with three disclosure levels:

- ``PublicGateEvidence`` -- run-level aggregates only (posture, the four gate
  summaries, flagged-case count and share, per-finding counts). Public.
- ``MinerGateNotesResponse`` -- the per-case notes, keyed by a stable
  ``note_id`` a dispute can cite. Owner only (miner session).
- ``scores.gate_evidence`` -- the stored projection (``StoredGateEvidence``),
  read back only through the owner and operator paths.

What the scorer actually emits (``dittobench-datagen/protocol`` json tags;
every field is additive-optional and absent below bench_version 13):

- per case: ``catalog.findings`` and ``catalog.tools_offered`` (catalog gate),
  ``claim_provenance.findings`` (claim-span / causal gate),
  ``inference_cost.factor_bps`` (shadow cost factor), ``relation`` (the
  generator's metamorphic relation), and the bare twin markers
  ``twin_concordant`` / ``counterfactual_insensitive`` in ``notes``;
- per run: ``details.catalog_gate``, ``details.claim_provenance``,
  ``details.twin_post_pass`` and ``details.inference_cost``.

Every published note is drawn from :data:`GATE_FINDINGS`, the Platform copy of
``services/dittobench-api/testdata/v13_gate_findings.json`` -- a token the
list does not know is dropped, never forwarded, so a scorer that grows a
value-bearing finding cannot leak it through this path. Relation names and
rule names are identifier-shaped slugs for the same reason.

Everything here is gated ``bench_version >= GATE_EVIDENCE_MIN_BENCH_VERSION``
as a floor: a v12 report stores ``NULL`` and its ``details`` stay
byte-identical.
"""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Floor, not an enumeration: every later bench version carries the evidence.
GATE_EVIDENCE_MIN_BENCH_VERSION = 13

# Version of the *stored* projection shape (``scores.gate_evidence``), so a
# later reshaping can tell old rows apart without a data migration.
GATE_EVIDENCE_CONTRACT_VERSION = 1

# ``observe`` is the twin post-pass's name for shadow; the other gates say
# ``shadow``. Both are carried verbatim per gate; the run-level ``posture``
# folds them (see ``ditto.api_server.gate_evidence.overall_posture``).
GatePosture = Literal["off", "shadow", "observe", "enforce"]

GateName = Literal["catalog", "claim_provenance", "twin_post_pass", "shared"]


class GateFinding(NamedTuple):
    """One scorer-emitted finding token: which gate emits it, and whether a
    settled instance zeroes the case when that gate runs in ``enforce``."""

    gate: GateName
    zeroing: bool
    # Findings that, when present on the same case, cancel the zero.
    waived_by: frozenset[str] = frozenset()


# The closed vocabulary of v13 per-case gate findings, byte-identical to what
# the Go scorer emits (``services/dittobench-api/testdata/v13_gate_findings.json``
# is the shared copy both sides pin). A note is published (to the owner) only
# when it is one of these, so the stored bytes never travel: the platform
# re-emits its own constant.
GATE_FINDINGS: MappingProxyType[str, GateFinding] = MappingProxyType(
    {
        # Catalog-present gate (relay-observed offered catalog; #1826).
        "restraint_without_offer": GateFinding("catalog", True),
        "expected_tool_not_offered": GateFinding(
            "catalog", True, frozenset({"semantic_preloading_safe_harbor"})
        ),
        "swallowed_model_call": GateFinding("catalog", True),
        "semantic_preloading_safe_harbor": GateFinding("catalog", False),
        "catalog_evidence_unavailable": GateFinding("catalog", False),
        "catalog_evidence_incomplete": GateFinding("catalog", False),
        "catalog_absent": GateFinding("catalog", False),
        "catalog_gate_zeroed": GateFinding("catalog", True),
        "memory_only_catalog": GateFinding("catalog", False),
        "offer_inferred_from_execution": GateFinding("catalog", False),
        "catalog_present_lower_bound": GateFinding("catalog", False),
        "claim_attribution_uncorroborated": GateFinding("catalog", False),
        # Emitted by both the catalog gate and the claim-span gate.
        "no_model_completion": GateFinding("shared", False),
        # Claim-span provenance + causal answer_in_prompt gate (#1849, #1833).
        "served_text_not_model_emitted": GateFinding("claim_provenance", True),
        "answer_in_prompt": GateFinding("claim_provenance", True),
        "claim_provenance_unavailable": GateFinding("claim_provenance", False),
        "claim_provenance_incomplete": GateFinding("claim_provenance", False),
        "claim_not_applicable": GateFinding("claim_provenance", False),
        "claim_provenance_zeroed": GateFinding("claim_provenance", True),
        # Twin decision rule and base/counterfactual pair (#1835), carried as
        # bare markers in ``CaseScore.notes``.
        "twin_concordant": GateFinding("twin_post_pass", True),
        "counterfactual_insensitive": GateFinding("twin_post_pass", True),
    }
)

GATE_NOTE_VOCABULARY: frozenset[str] = frozenset(GATE_FINDINGS)
GATE_ZEROING_NOTES: frozenset[str] = frozenset(
    note for note, finding in GATE_FINDINGS.items() if finding.zeroing
)
# The twin post-pass appends these to ``notes`` (followed by a free-text
# reason, which is never forwarded).
TWIN_NOTE_MARKERS: frozenset[str] = frozenset(
    note for note, finding in GATE_FINDINGS.items() if finding.gate == "twin_post_pass"
)

# Identifier-shaped: letters and underscores only, so no dataset value, count,
# or agent-supplied text can be smuggled through a relation or rule label.
_SLUG_PATTERN = r"^[a-z][a-z_]{0,47}$"
_NOTE_ID_PATTERN = r"^[0-9a-f]{16}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

_Slug = Annotated[str, Field(pattern=_SLUG_PATTERN)]
_UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]
_Count = Annotated[int, Field(ge=0)]
_BPS = Annotated[int, Field(ge=0, le=10_000)]


class CatalogGateSummary(BaseModel):
    """Run-level catalog-gate record (``details.catalog_gate``), sanitised.

    Counts, not rates, where they pool; ``catalog_suppression_rate`` is the
    published run-level metric (catalog absent / attributed tool cases).
    """

    model_config = ConfigDict(extra="ignore")

    posture: GatePosture | None = None
    tool_cases: _Count = 0
    attributed_cases: _Count = 0
    incomplete_capture_cases: _Count = 0
    lower_bound_cases: _Count = 0
    no_completion_cases: _Count = 0
    catalog_absent_cases: _Count = 0
    catalog_suppression_rate: _UnitFloat | None = None
    safe_harbor_cases: _Count = 0
    restraint_without_offer: _Count = 0
    expected_tool_not_offered: _Count = 0
    swallowed_model_call: _Count = 0
    zeroed_cases: _Count = 0
    claim_uncorroborated_cases: _Count = 0
    attribution_coverage_bps: _BPS | None = None


class ClaimProvenanceSummary(BaseModel):
    """Run-level claim-span / causal gate record (``details.claim_provenance``)."""

    model_config = ConfigDict(extra="ignore")

    posture: GatePosture | None = None
    memory_cases: _Count = 0
    attributed_cases: _Count = 0
    applicable_cases: _Count = 0
    settled_cases: _Count = 0
    not_model_emitted_cases: _Count = 0
    answer_in_prompt_cases: _Count = 0
    no_model_completion_cases: _Count = 0
    unsettled_cases: _Count = 0
    zeroed_cases: _Count = 0
    attribution_coverage_bps: _BPS | None = None


class TwinPostPassSummary(BaseModel):
    """Run-level twin / counterfactual post-pass record (``details.twin_post_pass``).

    ``rule_requested`` is the operator selection; ``rule`` is the rule that
    ran (they differ only after the calibration auto-fallback from
    ``concordant_zero`` to ``pair_product``). ``applied`` is true only when
    the posture was ``enforce`` and at least one score changed.
    """

    model_config = ConfigDict(extra="ignore")

    posture: GatePosture | None = None
    rule_requested: _Slug | None = None
    rule: _Slug | None = None
    honest_concordant_error_rate: _UnitFloat | None = None
    auto_fallback: bool = False
    twin_groups: _Count = 0
    twin_groups_concordant: _Count = 0
    counterfactual_pairs: _Count = 0
    counterfactual_insensitive: _Count = 0
    cases_affected: _Count = 0
    cases_affected_share: _UnitFloat | None = None
    applied: bool = False


class InferenceCostSummary(BaseModel):
    """Run-level shadow cost-factor record (``details.inference_cost``).

    ``mean_factor_bps`` is the mean factor the v13 cost rule WOULD have
    applied over the attributable cases; ``applied`` is always false in v13.0.
    """

    model_config = ConfigDict(extra="ignore")

    posture: GatePosture | None = None
    applied: bool = False
    floor_bps: _BPS | None = None
    cases: _Count = 0
    attributed_cases: _Count = 0
    cases_below_full_factor: _Count = 0
    mean_factor_bps: _BPS | None = None


class StoredGateCase(BaseModel):
    """One flagged case in the persisted projection (``scores.gate_evidence``).

    A case is stored when a gate would zero it at enforce (or did), or when
    the shadow cost factor would discount it. Notes are the case's findings
    from the closed vocabulary, zeroing and informational alike, in report
    order.
    """

    model_config = ConfigDict(extra="ignore")

    case_index: Annotated[int | None, Field(ge=0)] = None
    """Position in the report's ``per_case`` list."""

    case_id: Annotated[str | None, Field(max_length=200)] = None
    category: Annotated[str | None, Field(max_length=200)] = None
    kind: Annotated[str | None, Field(max_length=32)] = None
    score: _UnitFloat | None = None
    notes: list[str] = Field(default_factory=list, max_length=32)
    relation: _Slug | None = None
    cost_factor: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    tools_offered: Annotated[int | None, Field(ge=0, le=10_000)] = None
    catalog_present: bool | None = None


class StoredGateEvidence(BaseModel):
    """The persisted projection of one accepted score's gate evidence."""

    model_config = ConfigDict(extra="ignore")

    contract_version: Annotated[int, Field(ge=1)] = GATE_EVIDENCE_CONTRACT_VERSION
    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    posture: GatePosture | None = None
    catalog_gate: CatalogGateSummary | None = None
    claim_provenance: ClaimProvenanceSummary | None = None
    twin_post_pass: TwinPostPassSummary | None = None
    inference_cost: InferenceCostSummary | None = None
    catalog_suppression_rate: _UnitFloat | None = None
    gate_counts: dict[str, int] = Field(default_factory=dict)
    flagged_case_count: _Count = 0
    flagged_case_share: _UnitFloat | None = None
    cases: list[StoredGateCase] = Field(default_factory=list)


_POSTURE_DESCRIPTION = (
    "The most severe posture any v13 gate ran under: ``enforce`` means at least "
    "one gate changed scores; ``shadow`` means every gate only recorded what it "
    "would have done. Per-gate postures are on the gate summaries."
)
_FLAGGED_COUNT_DESCRIPTION = (
    "Cases a v13 gate would zero at enforce (or did), plus cases the shadow "
    "cost factor would discount."
)
_FLAGGED_SHARE_DESCRIPTION = "``flagged_case_count`` over the cases the run scored."
_GATE_COUNTS_DESCRIPTION = (
    "Gate finding -> number of cases it fired on (closed vocabulary; zeroing "
    "and informational findings alike)."
)


class PublicGateEvidence(BaseModel):
    """Run-level v13 gate verdict, published beside a validator's score.

    Aggregates only: the posture the gates ran under, the four gate summaries
    (counts and rates), how many cases the gates would zero, and per-finding
    counts. The per-case notes behind these counts are owner-only
    (``GET /me/agents/{agent_id}/gate-notes``).
    """

    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    posture: Annotated[
        GatePosture | None, Field(default=None, description=_POSTURE_DESCRIPTION)
    ] = None
    catalog_gate: CatalogGateSummary | None = None
    claim_provenance: ClaimProvenanceSummary | None = None
    twin_post_pass: TwinPostPassSummary | None = None
    inference_cost: InferenceCostSummary | None = None
    catalog_suppression_rate: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Share of attributed tool cases that offered no tool catalog.",
        ),
    ] = None
    flagged_case_count: Annotated[
        int, Field(ge=0, description=_FLAGGED_COUNT_DESCRIPTION)
    ] = 0
    flagged_case_share: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description=_FLAGGED_SHARE_DESCRIPTION),
    ] = None
    gate_counts: dict[str, int] = Field(
        default_factory=dict, description=_GATE_COUNTS_DESCRIPTION
    )


class MinerGateNote(BaseModel):
    """One gate note on one case, with the id a dispute can cite."""

    note_id: Annotated[str, Field(pattern=_NOTE_ID_PATTERN)]
    gate: Annotated[str, Field(pattern=_SLUG_PATTERN)]
    zeroing: Annotated[
        bool,
        Field(
            description=(
                "Whether this finding zeroes the case when its gate runs in "
                "enforce (a would-be zero in shadow)."
            )
        ),
    ]


class MinerGateNoteCase(BaseModel):
    """One flagged case's gate outcome, as shown to the owning miner."""

    case_index: Annotated[int | None, Field(ge=0)] = None
    case_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Seed-derived case id. The seed of every accepted score is "
                "already published on the submission's pipeline record, so the "
                "owner always sees it."
            ),
        ),
    ] = None
    category: str | None = None
    kind: str | None = None
    score: _UnitFloat | None = None
    notes: list[MinerGateNote] = Field(default_factory=list)
    relation: _Slug | None = None
    cost_factor: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    tools_offered: Annotated[int | None, Field(ge=0, le=10_000)] = None
    catalog_present: bool | None = None


class MinerGateNotesRun(BaseModel):
    """One validator run's gate verdict and its per-case notes (owner only)."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    run_id: str
    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    composite: _UnitFloat
    generated_at: datetime
    posture: GatePosture | None = None
    catalog_gate: CatalogGateSummary | None = None
    claim_provenance: ClaimProvenanceSummary | None = None
    twin_post_pass: TwinPostPassSummary | None = None
    inference_cost: InferenceCostSummary | None = None
    catalog_suppression_rate: _UnitFloat | None = None
    flagged_case_count: _Count = 0
    flagged_case_share: _UnitFloat | None = None
    gate_counts: dict[str, int] = Field(default_factory=dict)
    cases: list[MinerGateNoteCase] = Field(default_factory=list)


class MinerGateNotesResponse(BaseModel):
    """Every accepted run's v13 gate notes for one of the miner's own agents."""

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    runs: list[MinerGateNotesRun] = Field(default_factory=list)
    dispute_submit_url: Annotated[
        str,
        Field(
            description=(
                "Where the submission's one dispute is filed. A rejected "
                "submission disputes its quarantine decision; a scored, live, "
                "evaluating or held submission disputes the gate notes it cites "
                "by passing their ``note_id`` values as ``gate_note_ids``."
            )
        ),
    ] = "/api/v1/public/agent/{agent_id}/dispute"

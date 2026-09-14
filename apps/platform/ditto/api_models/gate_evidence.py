"""Bench v13 gate evidence: per-case gate notes and shadow verdicts.

Bench v13 adds score gates that can zero a case (catalog-present, claim-span
provenance, causal ``answer_in_prompt``, the twin / counterfactual pair rule)
and a shadow cost factor. Every one of them ships in **shadow** first: the
scorer computes what the gate *would* have done and reports it beside the
ungated composite without changing the score. A gate a miner cannot see is a
surprise zero at enforce with no evidence to appeal, so the scorer's per-case
notes and run-level shadow verdict are persisted on the accepted score and
exposed on three surfaces with three disclosure levels:

- ``PublicGateEvidence`` -- run-level aggregates only (posture, composite with
  and without gates, the gate-induced loss, per-gate counts). Public.
- ``MinerGateNotesResponse`` -- the per-case notes, keyed by a stable
  ``note_id`` a dispute can cite. Owner only (miner session).
- ``scores.gate_evidence`` -- the stored projection (``StoredGateEvidence``),
  read back only through the owner and operator paths.

Wire contract the scorer emits (``ScoreReport.details["gate_evidence"]``,
advisory and unsigned like every other ``details`` key):

.. code-block:: json

    {
      "posture": "shadow",
      "composite_with_gates": 0.61,
      "composite_without_gates": 0.87,
      "catalog_suppression_rate": 0.02,
      "cases": [
        {"case_id": "...", "notes": ["answer_in_prompt"],
         "relation": "decision_twin", "relation_outcome": "concordant_zero",
         "cost_factor": 1.0, "tools_offered": 5,
         "score_with_gates": 0.0, "score_without_gates": 1.0}
      ]
    }

Per-case gate notes may equally ride in ``per_case[].notes`` (the shared
``CaseScore`` wire shape is unchanged); the platform merges both by
``case_id``. Every note is drawn from :data:`GATE_NOTE_VOCABULARY` -- a note
the vocabulary does not know is dropped, never forwarded, so a scorer that
grows a value-bearing note cannot leak it through this path. Relation names
and outcomes are identifier-shaped slugs (letters and underscores only) for
the same reason.

Everything here is gated ``bench_version >= GATE_EVIDENCE_MIN_BENCH_VERSION``
as a floor: a v12 report stores ``NULL`` and its ``details`` stay
byte-identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Floor, not an enumeration: every later bench version carries the evidence.
GATE_EVIDENCE_MIN_BENCH_VERSION = 13

# Version of the *stored* projection shape (``scores.gate_evidence``), so a
# later reshaping can tell old rows apart without a data migration.
GATE_EVIDENCE_CONTRACT_VERSION = 1

GatePosture = Literal["off", "shadow", "enforce"]

# The closed vocabulary of v13 per-case gate notes. A note is published (to the
# owner) only when it is byte-identical to one of these, so the stored bytes
# never travel: the platform re-emits its own constant.
GATE_NOTE_VOCABULARY: frozenset[str] = frozenset(
    {
        # Catalog-present gate (relay-observed offered catalog; #1826).
        "restraint_without_offer",
        "expected_tool_not_offered",
        "swallowed_model_call",
        # Claim-span provenance gate (#1849) and the typed slot matcher.
        "served_text_not_model_emitted",
        "slot_not_in_prose",
        # Causal model-dependence gate (#1833).
        "answer_in_prompt",
        # Twin decision rule and base/counterfactual pair zero (#1835).
        "concordant_zero",
        "pair_product",
        "counterfactual_insensitive",
        # Shadow cost factor over choices + output tokens.
        "cost_factor_shadow",
    }
)

# Identifier-shaped: letters and underscores only, so no dataset value, count,
# or agent-supplied text can be smuggled through a relation label.
_SLUG_PATTERN = r"^[a-z][a-z_]{0,47}$"
_NOTE_ID_PATTERN = r"^[0-9a-f]{16}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

_Slug = Annotated[str, Field(pattern=_SLUG_PATTERN)]
_UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]


class ReportedGateCase(BaseModel):
    """One case's gate outcome as the scorer reports it (advisory input)."""

    model_config = ConfigDict(extra="ignore")

    case_id: Annotated[str, Field(min_length=1, max_length=200)]
    notes: list[str] = Field(default_factory=list, max_length=32)
    relation: _Slug | None = None
    relation_outcome: _Slug | None = None
    cost_factor: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    tools_offered: Annotated[int | None, Field(ge=0, le=10_000)] = None
    score_with_gates: _UnitFloat | None = None
    score_without_gates: _UnitFloat | None = None


class ReportedGateEvidence(BaseModel):
    """The run-level gate verdict as the scorer reports it (advisory input)."""

    model_config = ConfigDict(extra="ignore")

    posture: GatePosture | None = None
    composite_with_gates: _UnitFloat | None = None
    composite_without_gates: _UnitFloat | None = None
    catalog_suppression_rate: _UnitFloat | None = None
    cases: list[ReportedGateCase] = Field(default_factory=list)


class StoredGateCase(BaseModel):
    """One case in the persisted projection (``scores.gate_evidence``)."""

    model_config = ConfigDict(extra="ignore")

    case_index: Annotated[int | None, Field(ge=0)] = None
    """Position in the report's ``per_case`` list; ``None`` when the scorer
    reported a gate outcome for a case the breakdown does not carry."""

    case_id: Annotated[str | None, Field(max_length=200)] = None
    category: Annotated[str | None, Field(max_length=200)] = None
    kind: Annotated[str | None, Field(max_length=32)] = None
    score: _UnitFloat | None = None
    notes: list[str] = Field(default_factory=list, max_length=32)
    relation: _Slug | None = None
    relation_outcome: _Slug | None = None
    cost_factor: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    tools_offered: Annotated[int | None, Field(ge=0, le=10_000)] = None
    score_with_gates: _UnitFloat | None = None
    score_without_gates: _UnitFloat | None = None


class StoredGateEvidence(BaseModel):
    """The persisted projection of one accepted score's gate evidence."""

    model_config = ConfigDict(extra="ignore")

    contract_version: Annotated[int, Field(ge=1)] = GATE_EVIDENCE_CONTRACT_VERSION
    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    posture: GatePosture | None = None
    composite_with_gates: _UnitFloat | None = None
    composite_without_gates: _UnitFloat | None = None
    gate_induced_loss: _UnitFloat | None = None
    catalog_suppression_rate: _UnitFloat | None = None
    gate_counts: dict[str, int] = Field(default_factory=dict)
    relation_outcome_counts: dict[str, int] = Field(default_factory=dict)
    flagged_case_count: Annotated[int, Field(ge=0)] = 0
    cases: list[StoredGateCase] = Field(default_factory=list)


class PublicGateEvidence(BaseModel):
    """Run-level v13 gate verdict, published beside a validator's score.

    Aggregates only: the posture the gates ran under, the composite with and
    without them, the loss the gates would induce (``shadow``) or did induce
    (``enforce``), the catalog-suppression rate and per-gate counts. The
    per-case notes behind these counts are owner-only
    (``GET /me/agents/{agent_id}/gate-notes``).
    """

    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    posture: Annotated[
        GatePosture | None,
        Field(
            default=None,
            description=(
                "``shadow`` records what the gates would have done without "
                "changing the score; ``enforce`` means they did."
            ),
        ),
    ] = None
    composite_with_gates: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Composite after gates."),
    ] = None
    composite_without_gates: Annotated[
        float | None,
        Field(default=None, ge=0.0, le=1.0, description="Ungated composite."),
    ] = None
    gate_induced_loss: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description=(
                "``composite_without_gates - composite_with_gates``, clamped "
                "at zero. In shadow this is the loss enforce would introduce."
            ),
        ),
    ] = None
    catalog_suppression_rate: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.0,
            le=1.0,
            description="Share of deciding turns that offered no tool catalog.",
        ),
    ] = None
    flagged_case_count: Annotated[
        int, Field(ge=0, description="Cases carrying at least one gate note.")
    ] = 0
    gate_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Gate note -> number of cases it fired on (closed vocabulary).",
    )
    relation_outcome_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Twin / pair relation outcome -> number of cases.",
    )


class MinerGateNote(BaseModel):
    """One gate note on one case, with the id a dispute can cite."""

    note_id: Annotated[str, Field(pattern=_NOTE_ID_PATTERN)]
    gate: Annotated[str, Field(pattern=_SLUG_PATTERN)]


class MinerGateNoteCase(BaseModel):
    """One case's gate outcome, as shown to the owning miner."""

    case_index: Annotated[int | None, Field(ge=0)] = None
    case_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Seed-derived case id. Present only once the submission has "
                "settled into a public status, where its dataset seed is "
                "already published; null while the run is provisional."
            ),
        ),
    ] = None
    category: str | None = None
    kind: str | None = None
    score: _UnitFloat | None = None
    score_with_gates: _UnitFloat | None = None
    score_without_gates: _UnitFloat | None = None
    notes: list[MinerGateNote] = Field(default_factory=list)
    relation: _Slug | None = None
    relation_outcome: _Slug | None = None
    cost_factor: Annotated[float | None, Field(ge=0.0, le=100.0)] = None
    tools_offered: Annotated[int | None, Field(ge=0, le=10_000)] = None


class MinerGateNotesRun(BaseModel):
    """One validator run's gate verdict and its per-case notes (owner only)."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    run_id: str
    bench_version: Annotated[int, Field(ge=GATE_EVIDENCE_MIN_BENCH_VERSION)]
    composite: _UnitFloat
    generated_at: datetime
    posture: GatePosture | None = None
    composite_with_gates: _UnitFloat | None = None
    composite_without_gates: _UnitFloat | None = None
    gate_induced_loss: _UnitFloat | None = None
    catalog_suppression_rate: _UnitFloat | None = None
    flagged_case_count: Annotated[int, Field(ge=0)] = 0
    gate_counts: dict[str, int] = Field(default_factory=dict)
    relation_outcome_counts: dict[str, int] = Field(default_factory=dict)
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
                "Where a rejected submission's one dispute is filed; pass the "
                "``note_id`` values being contested as ``gate_note_ids``."
            )
        ),
    ] = "/api/v1/public/agent/{agent_id}/dispute"

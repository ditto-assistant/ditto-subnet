"""Per-mechanism scoring for DittoBench.

Python port of ``go/bittensor/scoring.go`` (in the same repo). Weight
constants are byte-identical with the Go source; parity is verified by
``ditto/tests/bench/test_scoring_core.py`` and ``test_scoring_retrieval.py``
against fixed input/expected-output cases lifted from the Go tests.

The Go source is the canonical scorer for on-chain weight setting and is
the implementation validators run in production. This Python port exists for
fast local feedback and for the contributor runner in
:mod:`ditto.bench.runner.run`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ditto.bench import SCHEMA_VERSION
from ditto.bench.loader.cases import RetrievalCase, ToolCallCase
from ditto.bench.loader.taxonomy import Mechanism, RetrievalCategory


@dataclass(slots=True)
class ToolCallScore:
    """Raw per-case tool-call metrics.

    Mirrors ``bittensor.ToolCallScore`` in ``go/bittensor/scoring.go``.
    ``name_f1`` is the
    multiset-level F1 between expected and observed tool names; ``arg_f1`` is
    the per-argument F1 computed by the arg-matcher engine.
    """

    name_precision: float = 0.0
    name_recall: float = 0.0
    name_f1: float = 0.0
    arg_f1: float = 0.0
    arg_matcher_score: float = 0.0
    trajectory_penalty: float = 0.0
    abstain_correct: bool = False
    score: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalScore:
    """Raw per-case retrieval IR metrics.

    Mirrors ``bittensor.RetrievalScore`` in ``go/bittensor/scoring.go``.
    """

    ndcg_5: float = 0.0
    ndcg_10: float = 0.0
    mrr: float = 0.0
    recall_5: float = 0.0
    recall_10: float = 0.0
    needle_hit: bool = False
    abstain_correct: bool = False
    contradiction_pass: bool = False
    num_relevant: int = 0
    num_returned: int = 0
    num_forbidden_hit: int = 0


@dataclass(slots=True)
class CoreScoreInputs:
    """Per-case observations the validator collects for a DittoCore challenge."""

    case: ToolCallCase
    tool: ToolCallScore
    latency_ms: int = 0
    budget_latency_ms: int = 0


@dataclass(slots=True)
class RetrievalScoreInputs:
    """Per-case observations the validator collects for a DittoRetrieval challenge.

    ``judge_score`` is only meaningful when ``judge_present`` is True (i.e.
    the challenge had ``include_answer=true`` and a judge model was run).
    """

    case: RetrievalCase
    retrieval: RetrievalScore
    judge_score: float = 0.0
    judge_present: bool = False
    used_tools: bool = False
    latency_ms: int = 0
    budget_latency_ms: int = 0
    mcp_parity_score: float = 0.0


@dataclass(slots=True)
class Score:
    """Per-case score record matching ``schemas/score.schema.json``."""

    schema_version: str
    case_id: str
    mechanism: Mechanism
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    category: str = ""
    domain: str = ""
    visibility: str = ""
    challenge_id: str = ""
    notes: list[str] = field(default_factory=list)
    graded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready dict using the canonical key names."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "challenge_id": self.challenge_id,
            "mechanism": str(self.mechanism),
            "case_id": self.case_id,
            "visibility": self.visibility,
            "category": self.category,
            "score": self.score,
            "breakdown": self.breakdown,
            "graded_at": self.graded_at.isoformat().replace("+00:00", "Z"),
        }
        if self.domain:
            out["domain"] = self.domain
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def latency_component(latency_ms: int, budget_ms: int) -> float:
    """Return the latency score component for a per-case observation.

    Returns 1.0 when latency is at or below the budget and decays linearly
    to 0 at 5x the budget. A non-positive budget or non-positive latency
    disables the component (returns 1.0). Matches the Go reference exactly.
    """
    if budget_ms <= 0 or latency_ms <= 0:
        return 1.0
    if latency_ms <= budget_ms:
        return 1.0
    excess = (latency_ms - budget_ms) / budget_ms
    score = 1.0 - excess / 4.0
    if score < 0:
        score = 0.0
    return score


def _clamp01(x: float) -> float:
    """Clamp ``x`` to ``[0, 1]``; NaN becomes 0."""
    if math.isnan(x):
        return 0.0
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x


def score_core(inputs: CoreScoreInputs) -> Score:
    """Compute a 0..1 DittoCore case score.

    Weight defaults (also documented in ``docs/coverage_matrix.md``)::

        0.50 tool_selection_f1
        0.25 arg_quality_f1
        0.15 sequence_score   (= 1 - trajectory_penalty)
        0.10 latency_score

    No-tool ("abstain") cases collapse ``tool_selection_f1`` to 1.0 when
    the miner correctly refused to call a tool and to 0.0 when any tool
    was invoked, so a single spurious tool call drops the case score.
    """
    seq_score = 1.0 - inputs.tool.trajectory_penalty
    if seq_score < 0:
        seq_score = 0.0

    latency_score = latency_component(inputs.latency_ms, inputs.budget_latency_ms)

    selection = inputs.tool.name_f1
    if len(inputs.case.expected_tools) == 0:
        selection = 1.0 if inputs.tool.abstain_correct else 0.0

    composite = (
        0.50 * selection
        + 0.25 * inputs.tool.arg_f1
        + 0.15 * seq_score
        + 0.10 * latency_score
    )
    composite = _clamp01(composite)

    return Score(
        schema_version=SCHEMA_VERSION,
        case_id=inputs.case.id,
        mechanism=Mechanism.CORE,
        score=composite,
        category=inputs.case.category,
        domain=inputs.case.domain,
        breakdown={
            "tool_selection_f1": selection,
            "arg_quality_f1": inputs.tool.arg_f1,
            "sequence_score": seq_score,
            "latency_score": latency_score,
        },
    )


def score_retrieval(inputs: RetrievalScoreInputs) -> Score:
    """Compute a 0..1 DittoRetrieval case score.

    Weight defaults (also documented in ``docs/coverage_matrix.md``)::

        0.45 evidence_metrics       (NDCG@5 + MRR + Recall@5 + NeedleHit)
        0.25 grounded_answer        (judge_score | exact_match)
        0.15 abstain_contradiction
        0.10 stm_ltm_routing
        0.05 latency_score

    ``mcp_parity`` is reported as a hard gate; failures below 0.9 generate
    an ``mcp_parity_below_gate`` note that surfaces on dashboards but does
    not reduce the composite directly. Validators may apply an additional
    discount to miners that consistently fail the gate.
    """
    r = inputs.retrieval

    evidence = 0.4 * r.ndcg_5 + 0.3 * r.mrr + 0.2 * r.recall_5
    if r.needle_hit:
        evidence += 0.1
    if evidence > 1:
        evidence = 1.0

    grounded = inputs.judge_score if inputs.judge_present else evidence

    abstain_contradiction = 0.0
    if r.abstain_correct:
        abstain_contradiction += 0.5
    if r.contradiction_pass:
        abstain_contradiction += 0.5
    if not inputs.case.forbidden_pair_ids and inputs.case.expected_pair_ids:
        # Cases that aren't abstention/contradiction get full credit on this
        # component so they aren't doubly penalised by the evidence metrics.
        abstain_contradiction = 1.0

    stm_ltm = 1.0
    if inputs.case.expect_no_tools and inputs.used_tools:
        stm_ltm = 0.0

    latency_score = latency_component(inputs.latency_ms, inputs.budget_latency_ms)

    composite = (
        0.45 * evidence
        + 0.25 * grounded
        + 0.15 * abstain_contradiction
        + 0.10 * stm_ltm
        + 0.05 * latency_score
    )
    composite = _clamp01(composite)

    notes: list[str] = []
    if (
        inputs.case.category == RetrievalCategory.MCP_PARITY
        and 0 < inputs.mcp_parity_score < 0.9
    ):
        notes.append("mcp_parity_below_gate")

    return Score(
        schema_version=SCHEMA_VERSION,
        case_id=inputs.case.id,
        mechanism=Mechanism.RETRIEVAL,
        score=composite,
        category=inputs.case.category,
        breakdown={
            "evidence_metrics": evidence,
            "grounded_answer": grounded,
            "abstain_contradiction": abstain_contradiction,
            "stm_ltm_routing": stm_ltm,
            "latency_score": latency_score,
            "mcp_parity": inputs.mcp_parity_score,
        },
        notes=notes,
    )

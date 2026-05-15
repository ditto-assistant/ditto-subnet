"""Parity tests for ditto.bench.runner.score_retrieval.

Each test mirrors a case in ``go/bittensor/scoring_test.go`` so the Python
port stays byte-identical with the canonical Go scorer.
"""

from __future__ import annotations

from ditto.bench.loader.cases import RetrievalCase
from ditto.bench.loader.taxonomy import Mechanism, RetrievalCategory
from ditto.bench.runner.scoring import (
    RetrievalScore,
    RetrievalScoreInputs,
    score_retrieval,
)


def test_score_retrieval_abstain_correct_credits_full_component() -> None:
    """A stale/abstain case with both signals gets full abstain_contradiction credit."""
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="x",
            category=RetrievalCategory.STALE_OUTSIDE_WINDOW,
            query="q",
            user_fixture_id="u",
        ),
        retrieval=RetrievalScore(abstain_correct=True, contradiction_pass=True),
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert s.mechanism is Mechanism.RETRIEVAL
    assert s.breakdown["abstain_contradiction"] == 1.0


def test_score_retrieval_stm_routing_violation_zeroes_component() -> None:
    """A stm_only case that called tools must lose the stm_ltm_routing component."""
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="stm",
            category=RetrievalCategory.STM_ONLY,
            query="q",
            user_fixture_id="u",
            expect_no_tools=True,
        ),
        retrieval=RetrievalScore(contradiction_pass=True),
        used_tools=True,
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert s.breakdown["stm_ltm_routing"] == 0.0


def test_score_retrieval_contradiction_pass_scores_high() -> None:
    """A perfect contradiction-update case scores >= 0.9."""
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="c",
            category=RetrievalCategory.CONTRADICTION_UPDATE,
            query="q",
            user_fixture_id="u",
            expected_pair_ids=["new"],
            forbidden_pair_ids=["old"],
        ),
        retrieval=RetrievalScore(
            ndcg_5=1.0,
            mrr=1.0,
            recall_5=1.0,
            needle_hit=True,
            contradiction_pass=True,
        ),
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert s.score >= 0.9


def test_score_retrieval_mcp_parity_below_gate_emits_note() -> None:
    """An MCP-parity case below 0.9 emits the mcp_parity_below_gate note."""
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="mcp",
            category=RetrievalCategory.MCP_PARITY,
            query="q",
            user_fixture_id="u",
            expected_pair_ids=["p1"],
        ),
        retrieval=RetrievalScore(ndcg_5=1.0, mrr=1.0, recall_5=1.0, needle_hit=True),
        mcp_parity_score=0.5,
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert "mcp_parity_below_gate" in s.notes
    assert s.breakdown["mcp_parity"] == 0.5


def test_score_retrieval_breakdown_keys_match_schema() -> None:
    """The Score breakdown must expose every key documented in scoring.md."""
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="x",
            category=RetrievalCategory.SINGLE_NEEDLE_RECENT,
            query="q",
            user_fixture_id="u",
            expected_pair_ids=["p1"],
        ),
        retrieval=RetrievalScore(ndcg_5=0.5, mrr=0.5, recall_5=0.5),
        latency_ms=500,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert set(s.breakdown) == {
        "evidence_metrics",
        "grounded_answer",
        "abstain_contradiction",
        "stm_ltm_routing",
        "latency_score",
        "mcp_parity",
    }


def test_score_retrieval_normal_case_gets_full_abstain_credit() -> None:
    """Non-abstention/non-contradiction cases get full abstain_contradiction credit.

    Mirrors the Go scorer behaviour that avoids double-penalising a normal
    retrieval case via the abstain/contradiction component when the case has
    expected pair IDs and no forbidden pair IDs.
    """
    inputs = RetrievalScoreInputs(
        case=RetrievalCase(
            id="x",
            category=RetrievalCategory.SINGLE_NEEDLE_RECENT,
            query="q",
            user_fixture_id="u",
            expected_pair_ids=["p1"],
        ),
        retrieval=RetrievalScore(
            ndcg_5=1.0,
            mrr=1.0,
            recall_5=1.0,
            needle_hit=True,
            abstain_correct=False,
            contradiction_pass=False,
        ),
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_retrieval(inputs)
    assert s.breakdown["abstain_contradiction"] == 1.0

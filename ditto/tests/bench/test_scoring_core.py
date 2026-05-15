"""Parity tests for ditto.bench.runner.score_core.

Each test mirrors a case in ``go/bittensor/scoring_test.go`` so the Python
port stays byte-identical with the canonical Go scorer.
"""

from __future__ import annotations

from ditto.bench.loader.cases import ExpectedToolCall, ToolCallCase
from ditto.bench.loader.taxonomy import CoreDomain, Mechanism
from ditto.bench.runner.scoring import (
    CoreScoreInputs,
    ToolCallScore,
    latency_component,
    score_core,
)


def test_score_core_perfect_case() -> None:
    """A perfect tool selection + arg quality scores near 1.0 on Mechanism.CORE."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(
            id="x",
            category="memory_lookup",
            prompt="",
            domain=CoreDomain.PERSONAL_RECALL,
            expected_tools=[ExpectedToolCall(name="search_memories")],
        ),
        tool=ToolCallScore(name_f1=1.0, arg_f1=1.0, trajectory_penalty=0.0),
        latency_ms=100,
        budget_latency_ms=1000,
    )
    s = score_core(inputs)
    assert s.mechanism is Mechanism.CORE
    assert s.score >= 0.99, f"expected near-perfect core score, got {s.score}"


def test_score_core_abstain_correct_collapses_selection_to_one() -> None:
    """An abstain-correct no-tool case collapses tool_selection_f1 to 1.0."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(
            id="abstain",
            category="no_tool",
            prompt="",
            domain=CoreDomain.TOOL_USE_ABSTENTION,
            expected_tools=[],
        ),
        tool=ToolCallScore(
            name_f1=0.0, arg_f1=1.0, trajectory_penalty=0.0, abstain_correct=True
        ),
        latency_ms=50,
        budget_latency_ms=1000,
    )
    s = score_core(inputs)
    assert s.breakdown["tool_selection_f1"] == 1.0
    assert s.score >= 0.99


def test_score_core_abstain_violation_zeroes_selection() -> None:
    """Calling any tool on a no-tool case zeroes tool_selection_f1."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(id="x", category="", prompt="", expected_tools=[]),
        tool=ToolCallScore(
            name_f1=0.0, arg_f1=1.0, trajectory_penalty=0.5, abstain_correct=False
        ),
        latency_ms=50,
        budget_latency_ms=1000,
    )
    s = score_core(inputs)
    assert s.breakdown["tool_selection_f1"] == 0.0


def test_score_core_breakdown_keys_match_schema() -> None:
    """The Score breakdown must expose every key documented in scoring.md."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(
            id="x",
            category="",
            prompt="",
            expected_tools=[ExpectedToolCall(name="t")],
        ),
        tool=ToolCallScore(name_f1=0.5, arg_f1=0.5, trajectory_penalty=0.1),
        latency_ms=500,
        budget_latency_ms=1000,
    )
    s = score_core(inputs)
    assert set(s.breakdown) == {
        "tool_selection_f1",
        "arg_quality_f1",
        "sequence_score",
        "latency_score",
    }


def test_latency_component_matches_go_reference() -> None:
    """Latency curve parity with the Go ``TestLatencyComponent`` reference."""
    assert latency_component(100, 1000) == 1.0
    assert latency_component(5000, 1000) == 0.0
    assert latency_component(0, 1000) == 1.0
    mid = latency_component(2000, 1000)
    assert 0.7 < mid < 0.8, f"expected ~0.75 mid-range, got {mid}"


def test_score_core_stamps_visibility() -> None:
    """``visibility`` flows from inputs to the resulting Score for aggregation."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(
            id="x",
            category="",
            prompt="",
            expected_tools=[ExpectedToolCall(name="t")],
        ),
        tool=ToolCallScore(name_f1=1.0, arg_f1=1.0),
        latency_ms=100,
        budget_latency_ms=1000,
        visibility="canary",
    )
    assert score_core(inputs).visibility == "canary"


def test_score_core_is_clamped_to_unit_interval() -> None:
    """Composite is always within [0, 1] even with degenerate inputs."""
    inputs = CoreScoreInputs(
        case=ToolCallCase(
            id="x",
            category="",
            prompt="",
            expected_tools=[ExpectedToolCall(name="t")],
        ),
        tool=ToolCallScore(name_f1=2.0, arg_f1=2.0, trajectory_penalty=-1.0),
        latency_ms=10,
        budget_latency_ms=1000,
    )
    s = score_core(inputs)
    assert 0.0 <= s.score <= 1.0

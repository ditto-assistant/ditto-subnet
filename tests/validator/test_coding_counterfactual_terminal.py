from __future__ import annotations

import pytest

from ditto.validator.coding_counterfactual_terminal import (
    CounterfactualTerminalResult,
    aggregate_counterfactual_results,
)


def _group(group_id: str, solved: set[str]) -> tuple[CounterfactualTerminalResult, ...]:
    return tuple(
        CounterfactualTerminalResult(group_id, condition, condition in solved)
        for condition in (
            "v0_none",
            "v1_relevant",
            "v2_irrelevant",
            "v3_stale_conflict",
            "v4_current_override",
        )
    )  # type: ignore[arg-type]


def test_terminal_reports_lift_but_uses_monotone_absolute_score() -> None:
    baseline = aggregate_counterfactual_results(
        _group(
            "group",
            {
                "v1_relevant",
                "v2_irrelevant",
                "v3_stale_conflict",
                "v4_current_override",
            },
        ),
        expected_group_ids=("group",),
    )
    improved = aggregate_counterfactual_results(
        _group(
            "group",
            {
                "v0_none",
                "v1_relevant",
                "v2_irrelevant",
                "v3_stale_conflict",
                "v4_current_override",
            },
        ),
        expected_group_ids=("group",),
    )
    assert baseline.useful_lift > improved.useful_lift
    assert improved.monotone_shadow_score > baseline.monotone_shadow_score


def test_terminal_counts_missing_and_untrusted_evidence_as_failure() -> None:
    result = aggregate_counterfactual_results(
        _group(
            "good",
            {
                "v0_none",
                "v1_relevant",
                "v2_irrelevant",
                "v3_stale_conflict",
                "v4_current_override",
            },
        )
        + (CounterfactualTerminalResult("bad", "v1_relevant", True, trusted=False),),
        expected_group_ids=("good", "bad"),
    )
    assert result.missing_result_count == 4
    assert result.untrusted_result_count == 1
    assert result.monotone_shadow_score < 1.0


def test_terminal_quarantine_cannot_raise_score() -> None:
    all_solved = {
        "v0_none",
        "v1_relevant",
        "v2_irrelevant",
        "v3_stale_conflict",
        "v4_current_override",
    }
    failed_as_zeros = aggregate_counterfactual_results(
        _group("good", all_solved) + _group("bad", set()),
        expected_group_ids=("good", "bad"),
    )
    quarantined_failed = aggregate_counterfactual_results(
        _group("good", all_solved) + _group("bad", set()),
        expected_group_ids=("good", "bad"),
        quarantined_group_ids=frozenset({"bad"}),
    )
    quarantined_passed = aggregate_counterfactual_results(
        _group("good", all_solved) + _group("bad", all_solved),
        expected_group_ids=("good", "bad"),
        quarantined_group_ids=frozenset({"bad"}),
    )
    assert quarantined_failed.quarantined_group_count == 1
    assert quarantined_failed.missing_result_count == 0
    assert (
        quarantined_failed.monotone_shadow_score
        == failed_as_zeros.monotone_shadow_score
    )
    assert (
        quarantined_passed.monotone_shadow_score
        == failed_as_zeros.monotone_shadow_score
    )
    assert quarantined_failed.monotone_shadow_score < 1.0


def test_terminal_rejects_non_boolean_evidence() -> None:
    with pytest.raises(ValueError, match="outside expected authority"):
        aggregate_counterfactual_results(
            (
                CounterfactualTerminalResult(
                    "good",
                    "v0_none",
                    1,  # type: ignore[arg-type]
                ),
            ),
            expected_group_ids=("good",),
        )

from __future__ import annotations

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


def test_terminal_quarantine_requires_explicit_expected_group() -> None:
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
        ),
        expected_group_ids=("good", "invalid"),
        quarantined_group_ids=frozenset({"invalid"}),
    )
    assert result.monotone_shadow_score == 1.0
    assert result.quarantined_group_count == 1

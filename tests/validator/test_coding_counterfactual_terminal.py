from __future__ import annotations

from ditto.validator.coding_counterfactual_terminal import (
    CounterfactualTerminalResult,
    aggregate_counterfactual_results,
)


def _group(group_id: str, solved: set[str]) -> tuple[CounterfactualTerminalResult, ...]:
    return tuple(
        CounterfactualTerminalResult(group_id, condition, condition in solved)
        for condition in ("v0", "v1", "v2", "v3", "v4")
    )  # type: ignore[arg-type]


def test_terminal_reports_lift_but_uses_monotone_absolute_score() -> None:
    baseline = aggregate_counterfactual_results(
        _group("group", {"v1", "v2", "v3", "v4"})
    )
    improved = aggregate_counterfactual_results(
        _group("group", {"v0", "v1", "v2", "v3", "v4"})
    )
    assert baseline.useful_lift > improved.useful_lift
    assert improved.monotone_shadow_score > baseline.monotone_shadow_score


def test_terminal_excludes_incomplete_untrusted_groups() -> None:
    result = aggregate_counterfactual_results(
        _group("good", {"v0", "v1", "v2", "v3", "v4"})
        + tuple(
            CounterfactualTerminalResult("bad", condition, True, trusted=False)
            for condition in ("v0", "v1", "v2", "v3", "v4")
        )
    )
    assert result.monotone_shadow_score == 1.0

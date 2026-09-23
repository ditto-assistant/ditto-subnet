"""Accuracy summaries for private source-review calibration runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def review_disposition(observation: object) -> str:
    """Keep incomplete source reviews distinct from certified clearances."""

    if not bool(getattr(observation, "ok", False)):
        return (
            "retryable_infra"
            if getattr(observation, "failure_disposition", None) == "retryable_infra"
            else "inconclusive"
        )
    risk = getattr(observation, "risk_level", None)
    if risk == "low":
        return (
            "safe"
            if getattr(observation, "clearance_certified", False) is True
            else "inconclusive"
        )
    if risk in {"medium", "high"}:
        return "violation"
    return "inconclusive"


def disposition_metrics(
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Report terminal errors without treating a hold as a safe classification."""

    outcomes = ("safe", "violation", "inconclusive", "retryable_infra")
    matrix = {
        expected: dict.fromkeys((*outcomes, "other"), 0)
        for expected in ("safe", "violation")
    }
    failure_codes: dict[str, int] = {}
    for item in items:
        expected = item.get("expected_disposition")
        if expected not in matrix:
            raise ValueError("expected_disposition must be safe or violation")
        actual = item.get("actual_disposition")
        matrix[expected][actual if actual in outcomes else "other"] += 1
        code = item.get("error_code")
        if isinstance(code, str) and code:
            failure_codes[code] = failure_codes.get(code, 0) + 1
    return {
        "matrix": matrix,
        "false_clears": matrix["violation"]["safe"],
        "false_holds": matrix["safe"]["violation"],
        "inconclusive": sum(matrix[expected]["inconclusive"] for expected in matrix),
        "retryable_infra": sum(
            matrix[expected]["retryable_infra"] for expected in matrix
        ),
        "failure_codes": dict(sorted(failure_codes.items())),
    }


def classification_metrics(
    items: Sequence[Mapping[str, object]],
) -> dict[str, int | float | None]:
    """Measure terminal classifications without crediting incomplete reviews.

    Recall uses every labeled violation in its denominator, so an incomplete
    review lowers detection rather than disappearing from the comparison.
    False-positive rate uses only terminally classified safe controls; coverage
    separately exposes the cases the reviewer could not classify.
    """

    true_positive = false_positive = true_negative = false_negative = 0
    unclassified_safe = unclassified_violation = 0
    for item in items:
        expected = item.get("expected_disposition")
        actual = item.get("actual_disposition")
        if expected not in {"safe", "violation"}:
            raise ValueError("expected_disposition must be safe or violation")
        if actual not in {"safe", "violation"}:
            if expected == "violation":
                unclassified_violation += 1
            else:
                unclassified_safe += 1
        elif expected == "violation" and actual == "violation":
            true_positive += 1
        elif expected == "safe" and actual == "violation":
            false_positive += 1
        elif expected == "safe":
            true_negative += 1
        else:
            false_negative += 1

    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative + unclassified_violation
    actual_negative = true_negative + false_positive + unclassified_safe
    classified_cases = true_positive + false_positive + true_negative + false_negative
    return {
        "cases": len(items),
        "classified_cases": classified_cases,
        "unclassified_safe": unclassified_safe,
        "unclassified_violation": unclassified_violation,
        "coverage": classified_cases / len(items) if items else None,
        "expected_violations": actual_positive,
        "expected_safe": actual_negative,
        "predicted_violations": predicted_positive,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "precision": (
            true_positive / predicted_positive if predicted_positive else None
        ),
        "recall": true_positive / actual_positive if actual_positive else None,
        "false_positive_rate": (
            false_positive / (false_positive + true_negative)
            if false_positive + true_negative
            else None
        ),
    }


__all__ = ["classification_metrics", "disposition_metrics", "review_disposition"]

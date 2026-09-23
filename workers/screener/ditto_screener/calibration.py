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
    """Measure violation precision/recall against operator-adjudicated labels.

    A non-violation model outcome, including ``inconclusive`` or
    ``retryable_infra``, is a negative classification. Operational outcome counts
    remain available on each result row; this summary measures only whether the
    court correctly separated confirmed violations from safe controls.
    """

    true_positive = false_positive = true_negative = false_negative = 0
    for item in items:
        expected = item.get("expected_disposition")
        actual = item.get("actual_disposition")
        if expected not in {"safe", "violation"}:
            raise ValueError("expected_disposition must be safe or violation")
        expected_violation = expected == "violation"
        predicted_violation = actual == "violation"
        if expected_violation and predicted_violation:
            true_positive += 1
        elif not expected_violation and predicted_violation:
            false_positive += 1
        elif not expected_violation and not predicted_violation:
            true_negative += 1
        else:
            false_negative += 1

    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative
    actual_negative = true_negative + false_positive
    return {
        "cases": len(items),
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
            false_positive / actual_negative if actual_negative else None
        ),
    }


__all__ = ["classification_metrics", "disposition_metrics", "review_disposition"]

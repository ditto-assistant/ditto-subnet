from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto_screener.calibration import (
    classification_metrics,
    disposition_metrics,
    review_disposition,
)


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (SimpleNamespace(ok=True, risk_level="low", clearance_certified=True), "safe"),
        (
            SimpleNamespace(ok=True, risk_level="low", clearance_certified=False),
            "inconclusive",
        ),
        (SimpleNamespace(ok=True, risk_level="medium"), "violation"),
        (
            SimpleNamespace(ok=False, failure_disposition="safe"),
            "inconclusive",
        ),
        (
            SimpleNamespace(ok=False, failure_disposition="violation"),
            "inconclusive",
        ),
        (
            SimpleNamespace(ok=False, failure_disposition="retryable_infra"),
            "retryable_infra",
        ),
    ],
)
def test_review_disposition_requires_completed_certified_clearance(
    observation: object, expected: str
) -> None:
    assert review_disposition(observation) == expected


def test_disposition_metrics_separates_errors_from_clearances() -> None:
    metrics = disposition_metrics(
        [
            {"expected_disposition": "violation", "actual_disposition": "safe"},
            {"expected_disposition": "safe", "actual_disposition": "violation"},
            {
                "expected_disposition": "safe",
                "actual_disposition": "inconclusive",
                "error_code": "stream-no-tool-call",
            },
            {
                "expected_disposition": "violation",
                "actual_disposition": "retryable_infra",
                "error_code": "completion-timeout",
            },
        ]
    )

    assert metrics["false_clears"] == 1
    assert metrics["false_holds"] == 1
    assert metrics["inconclusive"] == 1
    assert metrics["retryable_infra"] == 1
    assert metrics["matrix"]["safe"]["safe"] == 0
    assert metrics["failure_codes"] == {
        "completion-timeout": 1,
        "stream-no-tool-call": 1,
    }


def test_august_court_fixture_captures_precision_recall_baseline() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "source-review-court-calibration-2026-08-28.json"
        ).read_text()
    )
    cases = fixture["cases"]
    results = [
        {
            "expected_disposition": case["expected_disposition"],
            "actual_disposition": case["baseline_court_disposition"],
        }
        for case in cases
    ]

    assert {case["operator_outcome"] for case in cases} == {
        "confirmed_reject",
        "false_positive_release",
    }
    assert len(cases) == 6
    assert classification_metrics(results) == {
        "cases": 6,
        "classified_cases": 6,
        "unclassified_safe": 0,
        "unclassified_violation": 0,
        "coverage": 1.0,
        "expected_violations": 3,
        "expected_safe": 3,
        "predicted_violations": 6,
        "true_positive": 3,
        "false_positive": 3,
        "true_negative": 0,
        "false_negative": 0,
        "precision": 0.5,
        "recall": 1.0,
        "false_positive_rate": 1.0,
    }


def test_classification_metrics_does_not_credit_inconclusive_as_a_pass() -> None:
    metrics = classification_metrics(
        [
            {"expected_disposition": "safe", "actual_disposition": "inconclusive"},
            {
                "expected_disposition": "violation",
                "actual_disposition": "retryable_infra",
            },
        ]
    )

    assert metrics["true_negative"] == 0
    assert metrics["false_negative"] == 0
    assert metrics["unclassified_safe"] == 1
    assert metrics["unclassified_violation"] == 1
    assert metrics["classified_cases"] == 0
    assert metrics["coverage"] == 0.0
    assert metrics["precision"] is None
    assert metrics["recall"] == 0.0
    assert metrics["false_positive_rate"] is None


def test_classification_metrics_separates_false_clear_from_incomplete() -> None:
    metrics = classification_metrics(
        [
            {"expected_disposition": "safe", "actual_disposition": "safe"},
            {"expected_disposition": "safe", "actual_disposition": "inconclusive"},
            {"expected_disposition": "violation", "actual_disposition": "safe"},
            {
                "expected_disposition": "violation",
                "actual_disposition": "retryable_infra",
            },
        ]
    )

    assert metrics["true_negative"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["unclassified_safe"] == 1
    assert metrics["unclassified_violation"] == 1
    assert metrics["coverage"] == 0.5
    assert metrics["recall"] == 0.0
    assert metrics["false_positive_rate"] == 0.0


def test_classification_metrics_rejects_non_binary_gold_label() -> None:
    with pytest.raises(ValueError, match="expected_disposition"):
        classification_metrics(
            [
                {
                    "expected_disposition": "inconclusive",
                    "actual_disposition": "inconclusive",
                }
            ]
        )

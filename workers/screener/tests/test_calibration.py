from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto_screener.calibration import classification_metrics


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


def test_classification_metrics_treats_inconclusive_as_no_violation_verdict() -> None:
    metrics = classification_metrics(
        [
            {"expected_disposition": "safe", "actual_disposition": "inconclusive"},
            {
                "expected_disposition": "violation",
                "actual_disposition": "retryable_infra",
            },
        ]
    )

    assert metrics["true_negative"] == 1
    assert metrics["false_negative"] == 1
    assert metrics["precision"] is None
    assert metrics["recall"] == 0.0


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

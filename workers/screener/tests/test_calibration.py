from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto_screener.calibration import classification_metrics
from scripts.run_l2_calibration import (
    _apply_posture,
    _arguments,
    _load_posture,
    _report_only_audit_cost,
)


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


def test_report_only_cost_includes_paid_turns_before_infra_failure(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {
                    "event_type": "report_only_turn_usage",
                    "recorded_at": 99.0,
                    "reported_cost_usd": 9.0,
                },
                {
                    "event_type": "report_only_turn_usage",
                    "recorded_at": 100.0,
                    "reported_cost_usd": 0.4,
                },
                {
                    "event_type": "report_only_turn_contract_fault",
                    "recorded_at": 101.0,
                    "reported_cost_usd": 0.2,
                },
            )
        )
        + "\n"
    )

    assert _report_only_audit_cost(audit, started_at=100.0) == pytest.approx(0.6)


def _public_settings(tmp_path: Path) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "required_policy_version": 14,
                "postures": [
                    {
                        "scope": "*",
                        "role": "global",
                        "revision": 142,
                        "checksum": "ab" * 32,
                        "mode": "enforce",
                        "source_review_model": "openai/gpt-6-luna",
                        "source_review_max_steps": 240,
                        "source_review_timeout_seconds": 3600,
                        "source_review_max_read_bytes": 8000000,
                        "source_review_max_completion_tokens": 8000,
                        "source_review_reasoning_effort": "high",
                        "l2_model": "openai/gpt-6-sol",
                        "l2_fallback_models": ["z-ai/glm-5.2"],
                        "l3_enabled": True,
                        "l3_model": "openai/gpt-6-sol",
                        "max_steps": 256,
                        "timeout_seconds": 2700,
                        "max_input_tokens": 5000000,
                        "max_output_tokens": 1000000,
                        "max_completion_tokens": 16000,
                        "max_cost_usd": 25.0,
                        "critic_reasoning_effort": "medium",
                        "concern_hold_count": 3,
                        "clear_min_notes": 3,
                    },
                    {
                        "scope": "subnet-screener-1",
                        "role": "node_or_worker",
                        "mode": "inherit",
                    },
                ],
            }
        )
    )
    return path


def test_production_posture_overrides_local_review_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_l2_calibration.py",
            "--manifest",
            "m",
            "--artifact-root",
            "r",
            "--api-key-file",
            "k",
            "--analyzer-image",
            "i",
            "--results-file",
            "o",
        ],
    )
    args = _arguments()
    _apply_posture(args, _load_posture(str(_public_settings(tmp_path)), None))

    assert args.l1_max_steps == 240
    assert args.l1_timeout_seconds == 3600
    assert args.l1_model == "openai/gpt-6-luna"
    assert args.max_steps == 256
    assert args.timeout_seconds == 2700
    assert args.max_cost_usd == 25.0
    assert args.l2_model == "openai/gpt-6-sol"
    assert args.l2_fallback_models == ("z-ai/glm-5.2",)
    assert args.l3_model == "openai/gpt-6-sol"
    assert args.production_posture["revision"] == 142


def test_inheriting_scope_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="inherits"):
        _load_posture(str(_public_settings(tmp_path)), "subnet-screener-1")


def test_invalid_saved_posture_effort_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _public_settings(tmp_path)
    body = json.loads(path.read_text())
    body["postures"][0]["source_review_reasoning_effort"] = "maximum"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_l2_calibration.py",
            "--manifest",
            "m",
            "--artifact-root",
            "r",
            "--api-key-file",
            "k",
            "--analyzer-image",
            "i",
            "--results-file",
            "o",
        ],
    )
    args = _arguments()
    with pytest.raises(SystemExit, match="l1_reasoning_effort"):
        _apply_posture(args, _load_posture(str(path), None))


def _parsed_args(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_l2_calibration.py",
            "--manifest",
            "m",
            "--artifact-root",
            "r",
            "--api-key-file",
            "k",
            "--analyzer-image",
            "i",
            "--results-file",
            "o",
        ],
    )
    return _arguments()


def test_mistyped_saved_posture_field_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _public_settings(tmp_path)
    body = json.loads(path.read_text())
    body["postures"][0]["max_cost_usd"] = "5"
    path.write_text(json.dumps(body))
    args = _parsed_args(monkeypatch)
    with pytest.raises(SystemExit, match="max_cost_usd has an unexpected type"):
        _apply_posture(args, _load_posture(str(path), None))  # type: ignore[arg-type]


def test_report_only_canary_scope_is_refused(tmp_path: Path) -> None:
    path = _public_settings(tmp_path)
    body = json.loads(path.read_text())
    body["postures"].append(
        {"scope": "l2-report-canary", "role": "l2_report_canary", "mode": "enforce"}
    )
    path.write_text(json.dumps(body))
    with pytest.raises(SystemExit, match="report-only canary"):
        _load_posture(str(path), "l2-report-canary")

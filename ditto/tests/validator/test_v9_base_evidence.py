"""Typed and canonical Bench v9 base-evidence contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import (
    ScoreReport,
    V9BaseEvidence,
    V9ModelUseGate,
    V9ScoreGateEvidence,
)
from ditto.validator.signing import score_signing_message

_ARTIFACT = "ab" * 32
_DATASET = "cd" * 32
_TRANSCRIPT = "ef" * 32
_CROSS_LANGUAGE_VECTOR = (
    Path(__file__).resolve().parents[3]
    / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
)


def _gates() -> dict:
    return {
        "schema_version": 1,
        "bench_version": 9,
        "rollout_mode": "enforce",
        "threshold_profile": {
            "id": "v9-honest-v8-calibration-2026-08-08",
            "manifest_sha256": (
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        },
        "model_use": {
            "administered_cases": 12,
            "eligible_cases": 10,
            "successful_inference_cases": 9,
            "missing_inference_cases": 1,
            "observed_requests": 13,
            "successful_requests": 11,
            "prompt_tokens": 1100,
            "completion_tokens": 220,
            "excluded": {
                "preflight": 1,
                "ablation": 0,
                "undelivered": 1,
                "validator_fault": 0,
            },
            "case_attribution_complete": True,
            "request_coverage_bps": 10000,
            "coverage_bps": 9000,
            "threshold_bps": 9000,
            "result": "passed",
            "factor_bps": 10000,
        },
        "authoritative_tool": {
            "expected_executions": 10,
            "matched_executions": 9,
            "missing_executions": 1,
            "unexpected_executions": 0,
            "observed_executions": 9,
            "coverage_bps": 9000,
            "threshold_bps": 9000,
            "result": "passed",
            "factor_bps": 10000,
        },
    }


def _base() -> dict:
    gates = V9ScoreGateEvidence.model_validate(_gates())
    return {
        "schema_version": 1,
        "bench_version": 9,
        "score_contract": {
            "revision": "bench-v9-base-2026-08-08",
            "manifest_sha256": "12" * 32,
        },
        "run_id": "run-v9-vector",
        "artifact_sha256": _ARTIFACT,
        "dataset_sha256": _DATASET,
        "transcript_sha256": _TRANSCRIPT,
        "ordinary_composite_micros": 812345,
        "ordinary_stderr_micros": 12345,
        "score_gates": _gates(),
        "score_gates_sha256": gates.digest_hex(),
        "semantic_gate_factor_bps": 10000,
        "applied_gate_factor_bps": 10000,
        "effective_composite_micros": 812345,
        "effective_stderr_micros": 12345,
    }


def _report() -> dict:
    base = V9BaseEvidence.model_validate(_base())
    return {
        "run_id": "run-v9-vector",
        "bench_version": 9,
        "base_evidence_sha256": base.digest_hex(),
        "seed": 42,
        "composite": 0.812345,
        "raw_composite": 0.812345,
        "tool_mean": 0.9,
        "memory_mean": 0.75,
        "median_ms": 800,
        "n": 12,
        "composite_stderr": 0.012345,
        "generated_at": datetime(2026, 8, 8, tzinfo=UTC),
        "per_case": [],
        "structural_fingerprint": None,
        "details": {
            "dataset_sha256": _DATASET,
            "transcript_sha256": _TRANSCRIPT,
            "v9_base": _base(),
        },
    }


def test_score_gate_canonical_bytes_match_go_golden() -> None:
    evidence = V9ScoreGateEvidence.model_validate(_gates())
    assert evidence.digest_hex() == (
        "1247129dd087bb44443b040ad392bc13bc03b330701935a4885d1c11c36fadcb"
    )
    assert evidence.canonical_bytes().endswith(
        b"tool.result=passed\ntool.factor_bps=10000\n"
    )


def test_python_canonicalization_matches_shared_go_contract_vector() -> None:
    vector = json.loads(_CROSS_LANGUAGE_VECTOR.read_text())["vectors"][0]
    evidence = V9BaseEvidence.model_validate(vector["details"])
    assert (
        evidence.score_gates.canonical_bytes().decode()
        == vector["score_gates_canonical"]
    )
    assert evidence.score_gates.digest_hex() == vector["score_gates_sha256"]
    assert evidence.canonical_bytes().decode() == vector["base_canonical"]
    assert evidence.digest_hex() == vector["base_evidence_sha256"]
    signing = vector["signing"]
    assert (
        score_signing_message(
            validator_hotkey=signing["validator_hotkey"],
            agent_id=UUID(signing["agent_id"]),
            ticket_deadline=datetime.fromisoformat(signing["ticket_deadline"]),
            run_id=signing["run_id"],
            composite=signing["composite"],
            seed=signing["seed"],
            bench_version=signing["bench_version"],
            transcript_sha256=signing["transcript_sha256"],
            base_evidence_sha256=signing["base_evidence_sha256"],
        ).decode()
        == signing["message"]
    )


def test_v9_base_canonical_root_has_fixed_domain_order_and_trailing_newline() -> None:
    evidence = V9BaseEvidence.model_validate(_base())
    expected = (
        "ditto-v9-base-v1\n"
        "schema_version=1\n"
        "bench_version=9\n"
        "score_contract.revision=bench-v9-base-2026-08-08\n"
        f"score_contract.manifest_sha256={'12' * 32}\n"
        "run_id=run-v9-vector\n"
        f"artifact_sha256={_ARTIFACT}\n"
        f"dataset_sha256={_DATASET}\n"
        f"transcript_sha256={_TRANSCRIPT}\n"
        "ordinary_composite_micros=812345\n"
        "ordinary_stderr_micros=12345\n"
        f"score_gates_sha256={evidence.score_gates_sha256}\n"
        "semantic_gate_factor_bps=10000\n"
        "applied_gate_factor_bps=10000\n"
        "effective_composite_micros=812345\n"
        "effective_stderr_micros=12345\n"
    ).encode()
    assert evidence.canonical_bytes() == expected
    assert evidence.digest_hex() == hashlib.sha256(expected).hexdigest()


def test_score_report_accepts_complete_digest_verified_v9_evidence() -> None:
    report = ScoreReport.model_validate(_report())
    assert (
        report.base_evidence_sha256
        == V9BaseEvidence.model_validate(_base()).digest_hex()
    )


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("model_use", "missing_inference_cases"), 2, "missing inference"),
        (("model_use", "request_coverage_bps"), 9000, "derived evidence"),
        (("model_use", "coverage_bps"), 8999, "derived evidence"),
        (("model_use", "factor_bps"), 0, "derived evidence"),
        (("authoritative_tool", "missing_executions"), 2, "missing executions"),
        (("authoritative_tool", "observed_executions"), 10, "observed executions"),
        (("authoritative_tool", "factor_bps"), 0, "derived evidence"),
    ],
)
def test_derived_gate_tampering_is_rejected(
    path: tuple[str, str], value: object, match: str
) -> None:
    gates = _gates()
    gates[path[0]][path[1]] = value
    with pytest.raises(ValidationError, match=match):
        V9ScoreGateEvidence.model_validate(gates)


def test_repeated_aggregate_requests_cannot_fake_distinct_case_coverage() -> None:
    raw = _gates()["model_use"]
    raw.update(
        successful_inference_cases=0,
        missing_inference_cases=10,
        observed_requests=100,
        successful_requests=100,
        case_attribution_complete=False,
        request_coverage_bps=10000,
        coverage_bps=0,
        result="insufficient_evidence",
        factor_bps=0,
    )
    evidence = V9ModelUseGate.model_validate(raw)
    assert evidence.result == "insufficient_evidence"
    assert evidence.factor_bps == 0

    raw["result"] = "passed"
    raw["factor_bps"] = 10000
    with pytest.raises(ValidationError, match="derived evidence"):
        V9ModelUseGate.model_validate(raw)


def test_unexpected_tool_calls_are_diagnostic_not_a_gate_failure() -> None:
    raw = _gates()["authoritative_tool"]
    raw["unexpected_executions"] = 3
    raw["observed_executions"] = 12
    evidence = V9ScoreGateEvidence.model_validate(
        {**_gates(), "authoritative_tool": raw}
    )
    assert evidence.authoritative_tool.result == "passed"
    assert evidence.authoritative_tool.factor_bps == 10000


def test_shadow_preserves_application_but_publishes_failed_semantic_factor() -> None:
    raw = _base()
    raw["score_gates"]["rollout_mode"] = "shadow"
    raw["score_gates"]["model_use"].update(
        successful_inference_cases=0,
        missing_inference_cases=10,
        observed_requests=0,
        successful_requests=0,
        prompt_tokens=0,
        completion_tokens=0,
        case_attribution_complete=False,
        request_coverage_bps=0,
        coverage_bps=0,
        result="zero_inference",
        factor_bps=0,
    )
    gates = V9ScoreGateEvidence.model_validate(raw["score_gates"])
    raw["score_gates_sha256"] = gates.digest_hex()
    raw["semantic_gate_factor_bps"] = 0
    raw["applied_gate_factor_bps"] = 10000
    evidence = V9BaseEvidence.model_validate(raw)
    assert evidence.effective_composite_micros == 812345


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("run_id", "other-run", "run_id"),
        ("base_evidence_sha256", "00" * 32, "base_evidence_sha256"),
        ("composite", 0.7, "effective composite"),
        ("composite_stderr", 0.02, "effective stderr"),
    ],
)
def test_score_report_rejects_tampered_v9_identity(
    field: str, value: object, match: str
) -> None:
    report = _report()
    report[field] = value
    with pytest.raises(ValidationError, match=match):
        ScoreReport.model_validate(report)


@pytest.mark.parametrize("missing", ["base_evidence_sha256", "details"])
def test_v9_report_rejects_missing_mandatory_evidence(missing: str) -> None:
    report = _report()
    if missing == "details":
        report["details"].pop("v9_base")
    else:
        report.pop(missing)
    with pytest.raises(ValidationError, match="requires typed base evidence"):
        ScoreReport.model_validate(report)


def test_pre_v9_report_bytes_and_shape_reject_v9_only_fields() -> None:
    report = _report()
    report["bench_version"] = 8
    with pytest.raises(ValidationError, match="only valid for benchmark v9"):
        ScoreReport.model_validate(report)

    clean = copy.deepcopy(report)
    clean.pop("base_evidence_sha256")
    clean["details"].pop("v9_base")
    ScoreReport.model_validate(clean)

"""Regression tests for the canonical Bench v9 score-gate contract."""

from __future__ import annotations

import copy
import hashlib

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.bench_v9 import (
    V9BaseEvidence,
    V9ModelUseGate,
    V9ScoreGateEvidence,
)

_SHA = "ab" * 32


def _gates() -> dict[str, object]:
    return {
        "schema_version": 1,
        "bench_version": 9,
        "rollout_mode": "enforce",
        "threshold_profile": {"id": "v9-test", "manifest_sha256": _SHA},
        "model_use": {
            "administered_cases": 12,
            "eligible_cases": 10,
            "successful_inference_cases": 9,
            "missing_inference_cases": 1,
            "observed_requests": 11,
            "successful_requests": 10,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "excluded": {
                "preflight": 1,
                "ablation": 1,
                "undelivered": 0,
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
            "unexpected_executions": 2,
            "observed_executions": 11,
            "coverage_bps": 9000,
            "threshold_bps": 9000,
            "result": "passed",
            "factor_bps": 10000,
        },
    }


def _base(
    *, rollout_mode: str = "enforce", gate_passes: bool = True
) -> dict[str, object]:
    gates = _gates()
    gates["rollout_mode"] = rollout_mode
    if not gate_passes:
        model_use = gates["model_use"]
        assert isinstance(model_use, dict)
        model_use.update(
            successful_inference_cases=0,
            missing_inference_cases=10,
            observed_requests=0,
            successful_requests=0,
            prompt_tokens=0,
            completion_tokens=0,
            request_coverage_bps=0,
            coverage_bps=0,
            result="zero_inference",
            factor_bps=0,
        )
    evidence = V9ScoreGateEvidence.model_validate(gates)
    semantic_factor = 10000 if gate_passes else 0
    applied_factor = 10000 if rollout_mode == "shadow" else semantic_factor
    return {
        "schema_version": 1,
        "bench_version": 9,
        "score_contract": {"revision": "bench-v9-test", "manifest_sha256": _SHA},
        "run_id": "run-1",
        "artifact_sha256": _SHA,
        "dataset_sha256": "cd" * 32,
        "transcript_sha256": "ef" * 32,
        "ordinary_composite_micros": 812345,
        "ordinary_stderr_micros": 12345,
        "score_gates": gates,
        "score_gates_sha256": evidence.digest_hex(),
        "semantic_gate_factor_bps": semantic_factor,
        "applied_gate_factor_bps": applied_factor,
        "effective_composite_micros": 812345 if applied_factor else 0,
        "effective_stderr_micros": 12345 if applied_factor else 0,
    }


def test_score_gate_digest_is_canonical_and_pinned() -> None:
    evidence = V9ScoreGateEvidence.model_validate(_gates())

    assert evidence.canonical_bytes().startswith(b"ditto-score-gates-v1\n")
    assert evidence.canonical_bytes().endswith(
        b"tool.result=passed\ntool.factor_bps=10000\n"
    )
    assert (
        evidence.digest_hex() == hashlib.sha256(evidence.canonical_bytes()).hexdigest()
    )
    assert evidence.digest_hex() == (
        "7d7ccb23efb730b82e4c25aaf93197f831d6efcb0f99f857ca45421f943a0861"
    )


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("model_use", "eligible_cases", 9, "partition"),
        ("model_use", "missing_inference_cases", 2, "missing inference"),
        ("model_use", "successful_requests", 8, "successful inference"),
        ("model_use", "request_coverage_bps", 9000, "derived evidence"),
        ("model_use", "coverage_bps", 8999, "derived evidence"),
        ("model_use", "factor_bps", 0, "derived evidence"),
        ("authoritative_tool", "matched_executions", 11, "matched executions"),
        ("authoritative_tool", "missing_executions", 2, "missing executions"),
        ("authoritative_tool", "observed_executions", 10, "observed executions"),
        ("authoritative_tool", "coverage_bps", 8999, "derived evidence"),
        ("authoritative_tool", "factor_bps", 0, "derived evidence"),
    ],
)
def test_derived_gate_fields_cannot_be_forged(
    section: str, field: str, value: object, message: str
) -> None:
    raw = _gates()
    selected = raw[section]
    assert isinstance(selected, dict)
    selected[field] = value

    with pytest.raises(ValidationError, match=message):
        V9ScoreGateEvidence.model_validate(raw)


def test_repeated_requests_do_not_substitute_for_case_attribution() -> None:
    raw = _gates()["model_use"]
    assert isinstance(raw, dict)
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


@pytest.mark.parametrize(
    ("rollout_mode", "applied", "effective"),
    [("shadow", 10000, 812345), ("enforce", 0, 0)],
)
def test_rollout_mode_alone_controls_failed_gate_application(
    rollout_mode: str, applied: int, effective: int
) -> None:
    evidence = V9BaseEvidence.model_validate(
        _base(rollout_mode=rollout_mode, gate_passes=False)
    )

    assert evidence.semantic_gate_factor_bps == 0
    assert evidence.applied_gate_factor_bps == applied
    assert evidence.effective_composite_micros == effective


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("score_gates_sha256", "00" * 32, "score_gates_sha256"),
        ("semantic_gate_factor_bps", 0, "semantic gate factor"),
        ("applied_gate_factor_bps", 0, "applied gate factor"),
        ("effective_composite_micros", 0, "effective composite"),
        ("effective_stderr_micros", 0, "effective stderr"),
    ],
)
def test_base_evidence_rejects_tampered_derived_fields(
    field: str, value: object, message: str
) -> None:
    raw = _base()
    raw[field] = value

    with pytest.raises(ValidationError, match=message):
        V9BaseEvidence.model_validate(raw)


def test_contract_is_strict_and_frozen() -> None:
    raw = _base()
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        V9BaseEvidence.model_validate(raw)

    evidence = V9BaseEvidence.model_validate(_base())
    with pytest.raises(ValidationError, match="frozen"):
        evidence.run_id = "other"


def test_dump_and_reload_preserve_signature_bound_identity() -> None:
    evidence = V9BaseEvidence.model_validate(_base())
    reloaded = V9BaseEvidence.model_validate(copy.deepcopy(evidence.model_dump()))

    assert reloaded == evidence
    assert reloaded.canonical_bytes() == evidence.canonical_bytes()
    assert reloaded.digest_hex() == evidence.digest_hex()

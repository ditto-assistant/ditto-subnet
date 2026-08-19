"""Regression tests for the canonical Bench v9 score-gate contract."""

from __future__ import annotations

import copy
import hashlib
from typing import get_args

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.bench_v9 import (
    CONFIRMATION_BENCH_VERSIONS,
    V9_EVIDENCE_BENCH_VERSIONS,
    V9BaseEvidence,
    V9EvidenceBenchVersion,
    V9ModelUseGate,
    V9ScoreGateEvidence,
    normalize_v9_score_report_omitempty,
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


def test_go_omitempty_normalization_is_narrow_and_copy_on_write() -> None:
    raw = {
        "bench_version": 9,
        "details": {"v9_base": {"effective_stderr_micros": 0}},
    }

    normalized = normalize_v9_score_report_omitempty(raw)

    assert normalized == {**raw, "composite_stderr": 0.0}
    assert normalized is not raw
    assert "composite_stderr" not in raw


@pytest.mark.parametrize(
    "raw",
    [
        {"bench_version": 8, "details": {"v9_base": {"effective_stderr_micros": 0}}},
        {
            "bench_version": 9,
            "composite_stderr": None,
            "details": {"v9_base": {"effective_stderr_micros": 0}},
        },
        {"bench_version": 9, "details": {"v9_base": {"effective_stderr_micros": 1}}},
        {
            "bench_version": 9,
            "details": {"v9_base": {"effective_stderr_micros": False}},
        },
        {"bench_version": 9, "details": {}},
        {"bench_version": 9},
    ],
)
def test_go_omitempty_normalization_preserves_every_other_shape(
    raw: dict[str, object],
) -> None:
    assert normalize_v9_score_report_omitempty(raw) is raw


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


def test_contract_ignores_unknown_fields_and_is_frozen() -> None:
    raw = _base()
    raw["unexpected"] = True
    evidence = V9BaseEvidence.model_validate(raw)
    assert "unexpected" not in evidence.model_dump()

    with pytest.raises(ValidationError, match="frozen"):
        evidence.run_id = "other"


def test_dump_and_reload_preserve_signature_bound_identity() -> None:
    evidence = V9BaseEvidence.model_validate(_base())
    reloaded = V9BaseEvidence.model_validate(copy.deepcopy(evidence.model_dump()))

    assert reloaded == evidence
    assert reloaded.canonical_bytes() == evidence.canonical_bytes()
    assert reloaded.digest_hex() == evidence.digest_hex()


def test_v10_base_evidence_validates_with_matching_gate_version() -> None:
    base = _base()
    gates = copy.deepcopy(base["score_gates"])
    assert isinstance(gates, dict)
    gates["bench_version"] = 10
    evidence = V9ScoreGateEvidence.model_validate(gates)
    base.update(
        bench_version=10,
        score_gates=gates,
        score_gates_sha256=evidence.digest_hex(),
    )

    parsed = V9BaseEvidence.model_validate(base)

    assert parsed.bench_version == 10
    assert parsed.score_gates.bench_version == 10
    assert b"bench_version=10\n" in parsed.canonical_bytes()

    v9 = V9BaseEvidence.model_validate(_base())
    assert parsed.digest_hex() != v9.digest_hex()


def test_base_evidence_rejects_gate_bench_version_mismatch() -> None:
    base = _base()
    base["bench_version"] = 10

    with pytest.raises(ValidationError, match="bench_version"):
        V9BaseEvidence.model_validate(base)


@pytest.mark.parametrize("bench_version", V9_EVIDENCE_BENCH_VERSIONS)
def test_omitempty_normalization_covers_every_evidence_epoch(
    bench_version: int,
) -> None:
    """A zeroed gate must land as a zero score, not as a parse failure.

    Go omits a zero ``composite_stderr``. If the epoch is missing from the
    normalization set, ``ScoreReport`` rejects the report on both the validator
    and Platform, so the anti-emulation zero never lands and the caught agent
    keeps its previous ceiling. This is parametrized over the alias so it can
    never lag it again: a hand-written ``(9, 10, 11)`` here is what stranded
    bench 12 in exactly that state.
    """
    raw = {
        "bench_version": bench_version,
        "details": {"v9_base": {"effective_stderr_micros": 0}},
    }

    assert normalize_v9_score_report_omitempty(raw) == {
        **raw,
        "composite_stderr": 0.0,
    }


def test_omitempty_normalization_is_derived_from_the_alias() -> None:
    assert V9_EVIDENCE_BENCH_VERSIONS == get_args(V9EvidenceBenchVersion)
    assert CONFIRMATION_BENCH_VERSIONS is V9_EVIDENCE_BENCH_VERSIONS
    unsupported = max(V9_EVIDENCE_BENCH_VERSIONS) + 1
    raw = {
        "bench_version": unsupported,
        "details": {"v9_base": {"effective_stderr_micros": 0}},
    }

    assert normalize_v9_score_report_omitempty(raw) == raw


def test_omitempty_normalization_covers_v10_reports() -> None:
    raw = {
        "bench_version": 10,
        "details": {"v9_base": {"effective_stderr_micros": 0}},
    }

    assert normalize_v9_score_report_omitempty(raw) == {
        **raw,
        "composite_stderr": 0.0,
    }


def _v12_gates() -> dict[str, object]:
    gates = _gates()
    gates["bench_version"] = 12
    gates["model_dependence"] = {
        "administered_cases": 10,
        "eligible_cases": 10,
        "dependent_cases": 10,
        "independent_cases": 0,
        "slice_attribution_complete": True,
        "dependence_bps": 10000,
        "threshold_bps": 1,
        "result": "passed",
        "factor_bps": 10000,
    }
    return gates


def test_v12_gates_bind_into_the_signed_digest() -> None:
    v11 = copy.deepcopy(_gates())
    v11["bench_version"] = 11
    pre = V9ScoreGateEvidence.model_validate(v11)
    v12 = V9ScoreGateEvidence.model_validate(_v12_gates())

    assert b"model_dependence.result=passed\n" in v12.canonical_bytes()
    assert b"model_dependence.factor_bps=10000\n" in v12.canonical_bytes()
    assert v12.digest_hex() != pre.digest_hex()
    assert v12.combined_factor_bps() == 10000


def test_v12_score_rejects_a_stripped_digest() -> None:
    """The historical extra=ignore pin would drop v12 gates then hash v9 bytes."""
    stripped = copy.deepcopy(_v12_gates())
    stripped.pop("model_dependence")
    with pytest.raises(ValidationError, match="model_dependence"):
        V9ScoreGateEvidence.model_validate(stripped)


def test_v12_incomplete_counterfactual_fails_open() -> None:
    gates = _v12_gates()
    dependence = gates["model_dependence"]
    assert isinstance(dependence, dict)
    dependence.update(
        slice_attribution_complete=False,
        dependence_bps=0,
        result="insufficient_evidence",
        factor_bps=10000,
    )
    evidence = V9ScoreGateEvidence.model_validate(gates)
    assert evidence.model_dependence is not None
    assert evidence.model_dependence.result == "insufficient_evidence"
    assert evidence.model_dependence.factor_bps == 10000
    assert evidence.combined_factor_bps() == 10000

    dependence["factor_bps"] = 0
    with pytest.raises(ValidationError, match="model-dependence"):
        V9ScoreGateEvidence.model_validate(gates)


def test_v12_base_evidence_accepts_penalize_scaling() -> None:
    gates = _v12_gates()
    gates["answer_stuffing"] = {
        "administered_cases": 10,
        "eligible_cases": 10,
        "stuffed_cases": 2,
        "clean_cases": 8,
        "attribution_complete": True,
        "review_required": False,
        "posture": "penalize",
        "stuffed_bps": 2000,
        "threshold_bps": 1,
        "min_cases": 1,
        "loose_eligible_cases": 10,
        "loose_stuffed_cases": 2,
        "loose_stuffed_bps": 2000,
        "review_share_threshold_bps": 4000,
        "result": "answer_stuffed",
        "factor_bps": 8000,
    }
    evidence = V9ScoreGateEvidence.model_validate(gates)
    assert evidence.combined_factor_bps() == 8000
    assert b"answer_stuffing.posture=penalize\n" in evidence.canonical_bytes()
    base = _base()
    base.update(
        bench_version=12,
        score_gates=gates,
        score_gates_sha256=evidence.digest_hex(),
        semantic_gate_factor_bps=8000,
        applied_gate_factor_bps=8000,
        effective_composite_micros=649876,
        effective_stderr_micros=9876,
    )
    parsed = V9BaseEvidence.model_validate(base)
    assert parsed.effective_composite_micros == 649876
    assert parsed.applied_gate_factor_bps == 8000

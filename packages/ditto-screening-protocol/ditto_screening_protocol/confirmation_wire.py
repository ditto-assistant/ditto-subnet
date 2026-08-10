"""Strict shared adapters from Go Bench v9 evidence to normalized envelopes.

The scorer emits producer-native JSON: LongMem uses six-decimal floats and the
ablation contract includes trusted coordinator fields that are not public API
fields.  This module is the one explicit conversion boundary.  It verifies the
producer's canonical digest and exact field set before converting scores to the
Platform's integer micros/BPS representation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from ditto_screening_protocol.confirmation import (
    CAPABILITY_ORDER,
    AblationDimensionEnvelope,
    AblationEvidence,
    ConfirmationCompletionReport,
    LongMemDimensionEnvelope,
    LongMemEvidence,
    evidence_digest,
)

_FIXTURE_SCHEMA = "dittobench-v9-confirmation-go-evidence-v1"
_SHA256_LENGTH = 64
_SCORE_SCALE = 1_000_000
_FACTOR_SCALE = 10_000

_DIMENSION_KEYS = ("go_evidence_sha256", "latency_ms", "evidence")
_LONGMEM_KEYS = (
    "schema_version",
    "artifact_sha256",
    "bench_version",
    "profile_checksum",
    "case_set_digest",
    "dataset_revision",
    "dataset_sha256",
    "latency_ms",
    "score",
    "provider_evidence",
)
_LONGMEM_SCORE_KEYS = (
    "longmem_mean",
    "longmem_stderr",
    "case_count",
    "per_capability",
)
_CAPABILITY_KEYS = ("capability", "correct", "count", "mean")
_PROVIDER_KEYS = (
    "lane",
    "cost_source",
    "currency",
    "provider",
    "profile_revision",
    "model",
    "fallback_used",
    "requests",
    "successes",
    "receipted_requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd_micros",
    "receipt_set_sha256",
)
_ABLATION_KEYS = (
    "contract_version",
    "bench_version",
    "intervention",
    "mode",
    "status",
    "reason",
    "profile_revision",
    "artifact_sha256",
    "ablation_profile_sha256",
    "coordinator_sha256",
    "selected_cases_sha256",
    "threshold_manifest_sha256",
    "dataset_sha256",
    "case_set_sha256",
    "baseline_scores_sha256",
    "ablated_scores_sha256",
    "baseline_mean",
    "ablated_mean",
    "delta",
    "threshold",
    "sample_count",
    "affected_call_count",
    "semantic_factor",
    "applied_factor",
    "synthetic_usage",
)
_INFERENCE_BUDGET_KEYS = ("max_chat_requests", "max_chat_input_bytes")
_EMBEDDING_BUDGET_KEYS = (
    "max_embedding_requests",
    "max_embedding_inputs",
    "max_embedding_input_bytes",
)
_USAGE_PREFIX_KEYS = (
    "synthetic",
    "telemetry_namespace",
    "intervention",
    "budget",
)
_INFERENCE_USAGE_KEYS = (
    *_USAGE_PREFIX_KEYS,
    "chat_attempts",
    "chat_applied",
    "chat_input_bytes",
    "upstream_requests",
    "upstream_input_tokens",
    "upstream_output_tokens",
    "upstream_provider_cost_microusd",
)
_EMBEDDING_USAGE_KEYS = (
    *_USAGE_PREFIX_KEYS,
    "embedding_attempts",
    "embedding_applied",
    "embedding_inputs",
    "embedding_input_bytes",
    "upstream_requests",
    "upstream_input_tokens",
    "upstream_output_tokens",
    "upstream_provider_cost_microusd",
)


class ConfirmationWireError(ValueError):
    """Go evidence is malformed, stale, or not the frozen wire contract."""


def completion_report_from_go_fixture(
    raw: Mapping[str, object],
    *,
    ablation_coordinator_latency_ms: int,
    bundle_signature: str = "00",
) -> ConfirmationCompletionReport:
    """Convert one committed Go fixture into the exact Platform report model."""
    fixture = _ordered(
        raw,
        (
            "fixture_schema",
            "longmemeval",
            "inference_ablation",
            "embedding_ablation",
        ),
        "confirmation fixture",
    )
    if fixture["fixture_schema"] != _FIXTURE_SCHEMA:
        raise ConfirmationWireError("unsupported Go confirmation fixture schema")
    return completion_report_from_go_dimensions(
        ablation_coordinator_latency_ms=ablation_coordinator_latency_ms,
        longmemeval=_mapping(fixture["longmemeval"], "LongMem dimension"),
        inference_ablation=_mapping(
            fixture["inference_ablation"], "inference dimension"
        ),
        embedding_ablation=_mapping(
            fixture["embedding_ablation"], "embedding dimension"
        ),
        bundle_signature=bundle_signature,
    )


def completion_report_from_go_dimensions(
    *,
    ablation_coordinator_latency_ms: int,
    longmemeval: Mapping[str, object],
    inference_ablation: Mapping[str, object],
    embedding_ablation: Mapping[str, object],
    bundle_signature: str = "00",
) -> ConfirmationCompletionReport:
    """Convert the three native scorer wrappers used by the live transport."""
    if (
        isinstance(ablation_coordinator_latency_ms, bool)
        or not isinstance(ablation_coordinator_latency_ms, int)
        or ablation_coordinator_latency_ms < 0
    ):
        raise ConfirmationWireError(
            "ablation coordinator latency must be a nonnegative integer"
        )
    return ConfirmationCompletionReport(
        ablation_coordinator_latency_ms=ablation_coordinator_latency_ms,
        longmemeval=longmem_envelope_from_go(longmemeval),
        inference_ablation=ablation_envelope_from_go(
            inference_ablation,
            expected_intervention="inference",
        ),
        embedding_ablation=ablation_envelope_from_go(
            embedding_ablation,
            expected_intervention="embedding",
        ),
        bundle_signature=bundle_signature,
    )


def longmem_envelope_from_go(
    raw: Mapping[str, object],
) -> LongMemDimensionEnvelope:
    """Verify and convert a real ``longmemeval.Evidence`` v2 wrapper."""
    dimension = _ordered(raw, _DIMENSION_KEYS, "LongMem dimension")
    evidence_raw = _mapping(dimension["evidence"], "LongMem evidence")
    canonical = _canonical_longmem(evidence_raw)
    _verify_go_digest(canonical, dimension["go_evidence_sha256"], "LongMem")
    if _integer(canonical["schema_version"], "LongMem schema_version") != 2:
        raise ConfirmationWireError("LongMem evidence schema must be 2")
    if _integer(canonical["bench_version"], "LongMem bench_version") != 9:
        raise ConfirmationWireError("LongMem evidence must target bench_version 9")
    latency_ms = _integer(canonical["latency_ms"], "LongMem latency_ms")
    if latency_ms != _integer(dimension["latency_ms"], "LongMem envelope latency"):
        raise ConfirmationWireError("LongMem latency drift")

    score_raw = _mapping(canonical["score"], "LongMem score")
    capability_rows = [
        {
            "capability": _text(row["capability"], "LongMem capability"),
            "correct": _integer(row["correct"], "LongMem correct"),
            "count": _integer(row["count"], "LongMem count"),
            "mean_micros": _scaled(row["mean"], _SCORE_SCALE, "LongMem mean"),
        }
        for row in _mapping_sequence(
            score_raw["per_capability"], "LongMem capability rows"
        )
    ]
    if tuple(row["capability"] for row in capability_rows) != CAPABILITY_ORDER:
        raise ConfirmationWireError(
            "LongMem capabilities must appear once in frozen order"
        )
    provider_rows = [
        {key: row[key] for key in _PROVIDER_KEYS}
        for row in _mapping_sequence(
            canonical["provider_evidence"], "LongMem provider rows"
        )
    ]
    evidence = LongMemEvidence.model_validate(
        {
            "schema_version": 2,
            "artifact_sha256": canonical["artifact_sha256"],
            "bench_version": 9,
            "profile_checksum": canonical["profile_checksum"],
            "case_set_digest": canonical["case_set_digest"],
            "dataset_revision": canonical["dataset_revision"],
            "dataset_sha256": canonical["dataset_sha256"],
            "score": {
                "longmem_mean_micros": _scaled(
                    score_raw["longmem_mean"], _SCORE_SCALE, "LongMem macro mean"
                ),
                "longmem_stderr_micros": _scaled(
                    score_raw["longmem_stderr"],
                    _SCORE_SCALE,
                    "LongMem stderr",
                ),
                "case_count": _integer(score_raw["case_count"], "LongMem cases"),
                "per_capability": capability_rows,
            },
            "provider_evidence": provider_rows,
        }
    )
    return LongMemDimensionEnvelope(
        status="completed",
        evidence_sha256=evidence_digest(evidence),
        latency_ms=latency_ms,
        request_count=sum(row.requests for row in evidence.provider_evidence),
        input_tokens=sum(row.prompt_tokens for row in evidence.provider_evidence),
        output_tokens=sum(row.completion_tokens for row in evidence.provider_evidence),
        provider_cost_microusd=sum(
            row.cost_usd_micros for row in evidence.provider_evidence
        ),
        synthetic=False,
        evidence=evidence,
    )


def ablation_envelope_from_go(
    raw: Mapping[str, object],
    *,
    expected_intervention: str,
) -> AblationDimensionEnvelope:
    """Verify and convert one completed production ``ablation.Evidence``."""
    if expected_intervention not in {"inference", "embedding"}:
        raise ConfirmationWireError("unknown expected ablation intervention")
    dimension = _ordered(raw, _DIMENSION_KEYS, f"{expected_intervention} dimension")
    evidence_raw = _mapping(dimension["evidence"], f"{expected_intervention} evidence")
    canonical = _canonical_ablation(evidence_raw, expected_intervention)
    _verify_go_digest(
        canonical,
        dimension["go_evidence_sha256"],
        f"{expected_intervention} ablation",
    )
    if canonical["contract_version"] != "dittobench-v9-ablation-v1":
        raise ConfirmationWireError("unsupported Go ablation evidence contract")
    if _integer(canonical["bench_version"], "ablation bench_version") != 9:
        raise ConfirmationWireError("ablation evidence must target bench_version 9")
    if canonical["intervention"] != expected_intervention:
        raise ConfirmationWireError("ablation intervention drift")
    status = canonical["status"]
    if status not in {"failed", "unavailable"}:
        raise ConfirmationWireError("unsupported Go ablation evidence status")

    completed = status == "failed"

    usage_raw = _mapping(canonical["synthetic_usage"], "ablation synthetic usage")
    budget_raw = _mapping(usage_raw["budget"], "ablation synthetic budget")
    if usage_raw["telemetry_namespace"] != (
        f"dittobench.v9.ablation.{expected_intervention}"
    ):
        raise ConfirmationWireError("ablation telemetry namespace drift")
    budget = {
        "max_chat_requests": budget_raw.get("max_chat_requests", 0),
        "max_chat_input_bytes": budget_raw.get("max_chat_input_bytes", 0),
        "max_embedding_requests": budget_raw.get("max_embedding_requests", 0),
        "max_embedding_inputs": budget_raw.get("max_embedding_inputs", 0),
        "max_embedding_input_bytes": budget_raw.get("max_embedding_input_bytes", 0),
    }
    usage = {
        "synthetic": usage_raw["synthetic"],
        "intervention": usage_raw["intervention"],
        "budget": budget,
        "chat_attempts": usage_raw.get("chat_attempts", 0),
        "chat_applied": usage_raw.get("chat_applied", 0),
        "chat_input_bytes": usage_raw.get("chat_input_bytes", 0),
        "embedding_attempts": usage_raw.get("embedding_attempts", 0),
        "embedding_applied": usage_raw.get("embedding_applied", 0),
        "embedding_inputs": usage_raw.get("embedding_inputs", 0),
        "embedding_input_bytes": usage_raw.get("embedding_input_bytes", 0),
        "rejected_requests": usage_raw.get("rejected_requests", 0),
        "budget_exhausted": usage_raw.get("budget_exhausted", False),
        "upstream_requests": usage_raw["upstream_requests"],
        "upstream_input_tokens": usage_raw["upstream_input_tokens"],
        "upstream_output_tokens": usage_raw["upstream_output_tokens"],
        "upstream_provider_cost_microusd": usage_raw["upstream_provider_cost_microusd"],
    }
    evidence = AblationEvidence.model_validate(
        {
            "contract_version": canonical["contract_version"],
            "bench_version": 9,
            "artifact_sha256": canonical["artifact_sha256"],
            "intervention": canonical["intervention"],
            "mode": canonical["mode"],
            "status": canonical["status"],
            "reason": canonical["reason"],
            "profile_revision": canonical["profile_revision"],
            "profile_checksum": canonical["ablation_profile_sha256"],
            "threshold_manifest_sha256": canonical["threshold_manifest_sha256"],
            "coordinator_sha256": canonical["coordinator_sha256"],
            "dataset_sha256": canonical["dataset_sha256"],
            "case_set_sha256": canonical["case_set_sha256"],
            "baseline_scores_sha256": canonical.get("baseline_scores_sha256")
            if completed
            else None,
            "ablated_scores_sha256": canonical.get("ablated_scores_sha256")
            if completed
            else None,
            "baseline_mean_micros": _scaled(
                canonical["baseline_mean"], _SCORE_SCALE, "ablation baseline mean"
            )
            if completed
            else None,
            "ablated_mean_micros": _scaled(
                canonical["ablated_mean"], _SCORE_SCALE, "ablation ablated mean"
            )
            if completed
            else None,
            "delta_micros": _scaled(
                canonical["delta"], _SCORE_SCALE, "ablation delta", signed=True
            )
            if completed
            else None,
            "threshold_micros": _scaled(
                canonical["threshold"], _SCORE_SCALE, "ablation threshold"
            ),
            "sample_count": canonical["sample_count"],
            "affected_call_count": canonical["affected_call_count"],
            "semantic_factor_bps": _scaled(
                canonical["semantic_factor"],
                _FACTOR_SCALE,
                "ablation semantic factor",
            )
            if completed
            else None,
            "applied_factor_bps": _scaled(
                canonical["applied_factor"],
                _FACTOR_SCALE,
                "ablation applied factor",
            )
            if completed
            else None,
            "synthetic_usage": usage,
        }
    )
    return AblationDimensionEnvelope(
        status="completed" if completed else "unavailable",
        evidence_sha256=evidence_digest(evidence),
        latency_ms=_integer(dimension["latency_ms"], "ablation latency"),
        request_count=0,
        input_tokens=0,
        output_tokens=0,
        provider_cost_microusd=0,
        synthetic=True,
        evidence=evidence,
    )


def _canonical_longmem(raw: Mapping[str, object]) -> dict[str, object]:
    evidence = _ordered(raw, _LONGMEM_KEYS, "LongMem evidence")
    score = _ordered(
        _mapping(evidence["score"], "LongMem score"),
        _LONGMEM_SCORE_KEYS,
        "LongMem score",
    )
    score["per_capability"] = [
        _ordered(row, _CAPABILITY_KEYS, "LongMem capability row")
        for row in _mapping_sequence(score["per_capability"], "LongMem capability rows")
    ]
    evidence["score"] = score
    evidence["provider_evidence"] = [
        _ordered(row, _PROVIDER_KEYS, "LongMem provider row")
        for row in _mapping_sequence(
            evidence["provider_evidence"], "LongMem provider rows"
        )
    ]
    return evidence


def _canonical_ablation(
    raw: Mapping[str, object], intervention: str
) -> dict[str, object]:
    status = raw.get("status")
    evidence_keys: Sequence[str] = _ABLATION_KEYS
    if status == "unavailable":
        evidence_keys = tuple(
            key
            for key in _ABLATION_KEYS
            if key not in {"baseline_scores_sha256", "ablated_scores_sha256"}
        )
    evidence = _ordered(raw, evidence_keys, f"{intervention} ablation evidence")
    usage_keys: Sequence[str] = (
        _INFERENCE_USAGE_KEYS if intervention == "inference" else _EMBEDDING_USAGE_KEYS
    )
    budget_keys = (
        _INFERENCE_BUDGET_KEYS
        if intervention == "inference"
        else _EMBEDDING_BUDGET_KEYS
    )
    if "rejected_requests" in _mapping(
        evidence["synthetic_usage"], "ablation synthetic usage"
    ):
        insertion = usage_keys.index("upstream_requests")
        usage_keys = (
            *usage_keys[:insertion],
            "rejected_requests",
            "budget_exhausted",
            *usage_keys[insertion:],
        )
    usage = _ordered(
        _mapping(evidence["synthetic_usage"], "ablation synthetic usage"),
        usage_keys,
        "ablation synthetic usage",
    )
    usage["budget"] = _ordered(
        _mapping(usage["budget"], "ablation budget"),
        budget_keys,
        "ablation budget",
    )
    evidence["synthetic_usage"] = usage
    return evidence


def _verify_go_digest(payload: object, claimed: object, label: str) -> None:
    claimed_sha256 = _sha256(claimed, f"{label} Go evidence digest")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != claimed_sha256:
        raise ConfirmationWireError(f"{label} Go evidence digest mismatch")


def _ordered(
    raw: Mapping[str, object], keys: Sequence[str], label: str
) -> dict[str, object]:
    expected = set(keys)
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ConfirmationWireError(
            f"{label} fields drifted: missing={missing}, extra={extra}"
        )
    return {key: raw[key] for key in keys}


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfirmationWireError(f"{label} must be a JSON object")
    return value


def _mapping_sequence(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ConfirmationWireError(f"{label} must be a JSON array")
    return [_mapping(row, label) for row in value]


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfirmationWireError(f"{label} must be an integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfirmationWireError(f"{label} must be text")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ConfirmationWireError(f"{label} must be lowercase SHA-256")
    return text


def _scaled(
    value: object,
    scale: int,
    label: str,
    *,
    signed: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfirmationWireError(f"{label} must be a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfirmationWireError(f"{label} must be finite")
    decimal = Decimal(str(value)) * scale
    if decimal != decimal.to_integral_value():
        raise ConfirmationWireError(f"{label} exceeds six-decimal precision")
    result = int(decimal)
    minimum = -scale if signed else 0
    if result < minimum or result > scale:
        raise ConfirmationWireError(f"{label} is out of range")
    return result


__all__ = [
    "ConfirmationWireError",
    "ablation_envelope_from_go",
    "completion_report_from_go_dimensions",
    "completion_report_from_go_fixture",
    "longmem_envelope_from_go",
]

"""Canonical Bench v9 score-gate evidence shared by validator and Platform.

These models define signature-bound wire semantics. Keeping one implementation in
this monorepo prevents the validator and Platform from accepting different gate
derivations while retaining their existing public import paths.

The v9 gate contract carries forward unchanged to bench v10: the evidence keeps
the frozen v9 contract identity while ``bench_version`` records the run's actual
version, so digests remain version-bound.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_V9_PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9._:@/-]{0,127}$"

_V9_MAX_COUNT = 10_000_000
_V9_MAX_USAGE = 9_007_199_254_740_991
_BASIS_POINTS = 10_000

# The authoritative ordinary-score identity is shared by Platform admission,
# operator recovery, and validator evidence validation. Go binds the same pair
# through the cross-language contract vector; keeping the Python consumers on
# one definition prevents an operator retest from drifting from score intake.
V9_SCORE_CONTRACT_REVISION = "v9-base-enforce-efficiency-v1"
V9_SCORE_CONTRACT_MANIFEST_SHA256 = (
    "861d161cd031d5c40a4c50f0ae0c3d4a4f99a8513ff7fc87239f22104ebe3bb8"
)


def normalize_v9_score_report_omitempty(value: object) -> object:
    """Restore a signed zero stderr omitted by Go's JSON encoder.

    ``protocol.ScoreReport.CompositeStderr`` is additive-optional for historical
    benchmarks and therefore uses ``omitempty``.  An enforced v9 gate can reduce
    both the effective composite and its stderr to zero, causing Go to omit the
    top-level field even though the signature-bound v9 base evidence explicitly
    carries ``effective_stderr_micros = 0``.  Normalize only that exact v9 shape;
    pre-v9 reports, explicit nulls, non-zero evidence, and malformed evidence
    retain their existing validation behavior.
    """

    if not isinstance(value, Mapping):
        return value
    if value.get("bench_version") not in (9, 10) or "composite_stderr" in value:
        return value
    details = value.get("details")
    if not isinstance(details, Mapping):
        return value
    evidence = details.get("v9_base")
    if not isinstance(evidence, Mapping):
        return value
    effective_stderr = evidence.get("effective_stderr_micros")
    if type(effective_stderr) is not int or effective_stderr != 0:
        return value
    normalized = dict(value)
    normalized["composite_stderr"] = 0.0
    return normalized


class V9ScoreContract(BaseModel):
    """Frozen identity of the ordinary v9 score contract."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Annotated[str, Field(pattern=_V9_PROFILE_ID_PATTERN)]
    manifest_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]


class V9ThresholdProfile(BaseModel):
    """Frozen identity of the calibrated v9 gate thresholds."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[str, Field(pattern=_V9_PROFILE_ID_PATTERN)]
    manifest_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]


class V9GateExclusions(BaseModel):
    """Cases excluded from the model-use denominator for trusted reasons."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    preflight: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    ablation: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    undelivered: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    validator_fault: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]

    def total(self) -> int:
        return self.preflight + self.ablation + self.undelivered + self.validator_fault


V9GateResult = Literal[
    "passed",
    "below_threshold",
    "zero_inference",
    "insufficient_evidence",
    "not_applicable",
]


class V9ModelUseGate(BaseModel):
    """Trusted relay evidence for the v9 model-use binary gate."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    administered_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    eligible_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    successful_inference_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    missing_inference_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    observed_requests: Annotated[int, Field(ge=0, le=_V9_MAX_USAGE)]
    successful_requests: Annotated[int, Field(ge=0, le=_V9_MAX_USAGE)]
    prompt_tokens: Annotated[int, Field(ge=0, le=_V9_MAX_USAGE)]
    completion_tokens: Annotated[int, Field(ge=0, le=_V9_MAX_USAGE)]
    excluded: V9GateExclusions
    case_attribution_complete: bool
    request_coverage_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    coverage_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    threshold_bps: Annotated[int, Field(ge=1, le=_BASIS_POINTS)]
    result: V9GateResult
    factor_bps: Literal[0, 10000]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V9ModelUseGate:
        if self.eligible_cases + self.excluded.total() != self.administered_cases:
            raise ValueError("eligible cases and exclusions must partition cases")
        if self.successful_inference_cases > self.eligible_cases:
            raise ValueError("successful inference cases exceed eligible cases")
        if self.missing_inference_cases != (
            self.eligible_cases - self.successful_inference_cases
        ):
            raise ValueError("missing inference cases are not derived correctly")
        if self.successful_requests > self.observed_requests:
            raise ValueError("successful requests exceed observed requests")
        if self.successful_inference_cases > self.successful_requests:
            raise ValueError("successful inference cases exceed successful requests")
        if self.successful_requests > 0 and self.prompt_tokens == 0:
            raise ValueError("successful requests require prompt token accounting")
        if self.successful_requests == 0 and (
            self.prompt_tokens != 0 or self.completion_tokens != 0
        ):
            raise ValueError("tokens require a successful request")
        request_coverage = (
            _BASIS_POINTS
            if self.eligible_cases == 0
            else min(self.successful_requests, self.eligible_cases)
            * _BASIS_POINTS
            // self.eligible_cases
        )
        if self.eligible_cases == 0:
            coverage = _BASIS_POINTS
            result: V9GateResult = "not_applicable"
            factor = _BASIS_POINTS
        elif (
            self.observed_requests == 0
            and self.successful_requests == 0
            and self.successful_inference_cases == 0
            and self.prompt_tokens == 0
            and self.completion_tokens == 0
        ):
            coverage, result, factor = 0, "zero_inference", 0
        elif not self.case_attribution_complete:
            if self.successful_inference_cases != 0:
                raise ValueError(
                    "successful inference cases require complete attribution"
                )
            coverage, result, factor = 0, "insufficient_evidence", 0
        else:
            coverage = (
                self.successful_inference_cases * _BASIS_POINTS // self.eligible_cases
            )
            if coverage < self.threshold_bps:
                result, factor = "below_threshold", 0
            else:
                result, factor = "passed", _BASIS_POINTS
        if (
            self.request_coverage_bps,
            self.coverage_bps,
            self.result,
            self.factor_bps,
        ) != (
            request_coverage,
            coverage,
            result,
            factor,
        ):
            raise ValueError("model-use derived evidence is inconsistent")
        return self


class V9AuthoritativeToolGate(BaseModel):
    """Trusted tool-server evidence for the v9 authoritative-tool gate."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    expected_executions: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    matched_executions: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    missing_executions: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    unexpected_executions: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    observed_executions: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    coverage_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    threshold_bps: Annotated[int, Field(ge=1, le=_BASIS_POINTS)]
    result: V9GateResult
    factor_bps: Literal[0, 10000]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V9AuthoritativeToolGate:
        if self.matched_executions > self.expected_executions:
            raise ValueError("matched executions exceed expected executions")
        if self.missing_executions != (
            self.expected_executions - self.matched_executions
        ):
            raise ValueError("missing executions are not derived correctly")
        if self.observed_executions != (
            self.matched_executions + self.unexpected_executions
        ):
            raise ValueError("observed executions are not derived correctly")
        coverage = (
            _BASIS_POINTS
            if self.expected_executions == 0
            else self.matched_executions * _BASIS_POINTS // self.expected_executions
        )
        if self.expected_executions == 0:
            result: V9GateResult = "not_applicable"
            factor = _BASIS_POINTS
        elif coverage < self.threshold_bps:
            result, factor = "below_threshold", 0
        else:
            result, factor = "passed", _BASIS_POINTS
        if (self.coverage_bps, self.result, self.factor_bps) != (
            coverage,
            result,
            factor,
        ):
            raise ValueError("authoritative-tool derived evidence is inconsistent")
        return self


class V9ScoreGateEvidence(BaseModel):
    """Complete typed mirror of ``internal/scoregates.Evidence``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1]
    bench_version: Literal[9, 10]
    rollout_mode: Literal["shadow", "enforce"]
    threshold_profile: V9ThresholdProfile
    model_use: V9ModelUseGate
    authoritative_tool: V9AuthoritativeToolGate

    def canonical_bytes(self) -> bytes:
        model = self.model_use
        excluded = model.excluded
        tool = self.authoritative_tool
        return (
            "ditto-score-gates-v1\n"
            f"schema_version={self.schema_version}\n"
            f"bench_version={self.bench_version}\n"
            f"rollout_mode={self.rollout_mode}\n"
            f"threshold_profile.id={self.threshold_profile.id}\n"
            "threshold_profile.manifest_sha256="
            f"{self.threshold_profile.manifest_sha256}\n"
            f"model.administered_cases={model.administered_cases}\n"
            f"model.eligible_cases={model.eligible_cases}\n"
            "model.successful_inference_cases="
            f"{model.successful_inference_cases}\n"
            f"model.missing_inference_cases={model.missing_inference_cases}\n"
            f"model.observed_requests={model.observed_requests}\n"
            f"model.successful_requests={model.successful_requests}\n"
            f"model.prompt_tokens={model.prompt_tokens}\n"
            f"model.completion_tokens={model.completion_tokens}\n"
            f"model.excluded.preflight={excluded.preflight}\n"
            f"model.excluded.ablation={excluded.ablation}\n"
            f"model.excluded.undelivered={excluded.undelivered}\n"
            f"model.excluded.validator_fault={excluded.validator_fault}\n"
            "model.case_attribution_complete="
            f"{str(model.case_attribution_complete).lower()}\n"
            f"model.request_coverage_bps={model.request_coverage_bps}\n"
            f"model.coverage_bps={model.coverage_bps}\n"
            f"model.threshold_bps={model.threshold_bps}\n"
            f"model.result={model.result}\n"
            f"model.factor_bps={model.factor_bps}\n"
            f"tool.expected_executions={tool.expected_executions}\n"
            f"tool.matched_executions={tool.matched_executions}\n"
            f"tool.missing_executions={tool.missing_executions}\n"
            f"tool.unexpected_executions={tool.unexpected_executions}\n"
            f"tool.observed_executions={tool.observed_executions}\n"
            f"tool.coverage_bps={tool.coverage_bps}\n"
            f"tool.threshold_bps={tool.threshold_bps}\n"
            f"tool.result={tool.result}\n"
            f"tool.factor_bps={tool.factor_bps}\n"
        ).encode()

    def digest_hex(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class V9BaseEvidence(BaseModel):
    """Signature-bound ordinary v9 score identity and binary gate evidence."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1]
    bench_version: Literal[9, 10]
    score_contract: V9ScoreContract
    run_id: Annotated[str, Field(min_length=1)]
    artifact_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    dataset_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    transcript_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    ordinary_composite_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    ordinary_stderr_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    score_gates: V9ScoreGateEvidence
    score_gates_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    semantic_gate_factor_bps: Literal[0, 10000]
    applied_gate_factor_bps: Literal[0, 10000]
    effective_composite_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    effective_stderr_micros: Annotated[int, Field(ge=0, le=1_000_000)]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V9BaseEvidence:
        if self.bench_version != self.score_gates.bench_version:
            raise ValueError("score_gates bench_version does not match base evidence")
        if self.score_gates_sha256 != self.score_gates.digest_hex():
            raise ValueError("score_gates_sha256 does not match score_gates")
        semantic = (
            0
            if self.score_gates.model_use.factor_bps == 0
            or self.score_gates.authoritative_tool.factor_bps == 0
            else _BASIS_POINTS
        )
        applied = (
            _BASIS_POINTS if self.score_gates.rollout_mode == "shadow" else semantic
        )
        if self.semantic_gate_factor_bps != semantic:
            raise ValueError("semantic gate factor contradicts score-gate evidence")
        if self.applied_gate_factor_bps != applied:
            raise ValueError("applied gate factor contradicts rollout mode")
        expected_composite = self.ordinary_composite_micros if applied else 0
        expected_stderr = self.ordinary_stderr_micros if applied else 0
        if self.effective_composite_micros != expected_composite:
            raise ValueError("effective composite contradicts applied gate factor")
        if self.effective_stderr_micros != expected_stderr:
            raise ValueError("effective stderr contradicts applied gate factor")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            "ditto-v9-base-v1\n"
            f"schema_version={self.schema_version}\n"
            f"bench_version={self.bench_version}\n"
            f"score_contract.revision={self.score_contract.revision}\n"
            "score_contract.manifest_sha256="
            f"{self.score_contract.manifest_sha256}\n"
            f"run_id={self.run_id}\n"
            f"artifact_sha256={self.artifact_sha256}\n"
            f"dataset_sha256={self.dataset_sha256}\n"
            f"transcript_sha256={self.transcript_sha256}\n"
            f"ordinary_composite_micros={self.ordinary_composite_micros}\n"
            f"ordinary_stderr_micros={self.ordinary_stderr_micros}\n"
            f"score_gates_sha256={self.score_gates_sha256}\n"
            f"semantic_gate_factor_bps={self.semantic_gate_factor_bps}\n"
            f"applied_gate_factor_bps={self.applied_gate_factor_bps}\n"
            f"effective_composite_micros={self.effective_composite_micros}\n"
            f"effective_stderr_micros={self.effective_stderr_micros}\n"
        ).encode()

    def digest_hex(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

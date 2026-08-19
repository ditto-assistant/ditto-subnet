"""Canonical Bench v9 score-gate evidence shared by validator and Platform.

These models define signature-bound wire semantics. Keeping one implementation in
this monorepo prevents the validator and Platform from accepting different gate
derivations while retaining their existing public import paths.

The v9 gate contract carries forward unchanged to bench v10 and v11: the
evidence keeps the frozen v9 contract identity while ``bench_version`` records
the run's actual version, so digests remain version-bound. Bench v12 appends
the causal model-dependence, inference-latency, and answer-stuffing gates to
the same signed digest; Python must re-derive those bytes or Platform rejects
every v12 score.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_V9_PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9._:@/-]{0,127}$"

_V9_MAX_COUNT = 10_000_000
_V9_MAX_USAGE = 9_007_199_254_740_991
_BASIS_POINTS = 10_000
_ANSWER_STUFFING_MAX_PENALTY_BPS = 5_000
_ANSWER_STUFFING_REVIEW_MIN_CASES = 2

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
    if (
        value.get("bench_version") not in V9_EVIDENCE_BENCH_VERSIONS
        or "composite_stderr" in value
    ):
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
    "latency_implausible",
    "answer_stuffed",
    "review_required",
]


def _coverage_bps(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return numerator * _BASIS_POINTS // denominator


def _go_bool(value: bool) -> str:
    return "true" if value else "false"


def _apply_gate_factor_micros(ordinary_micros: int, factor_bps: int) -> int:
    """Match Go ``ApplyForVersion`` then ``scoreMicros`` (half away from zero)."""
    if factor_bps == 0:
        return 0
    if factor_bps >= _BASIS_POINTS:
        return ordinary_micros
    scaled = ordinary_micros * factor_bps / _BASIS_POINTS
    if scaled >= 0:
        return int(math.floor(scaled + 0.5))
    return int(math.ceil(scaled - 0.5))

V9EvidenceBenchVersion = Literal[9, 10, 11, 12]
"""Benchmark epochs whose scores carry the signed v9 base-evidence stack.

Every layer that parses, re-derives, or *projects* that evidence must pin this
alias rather than restate the versions: the stack was carried forward to v10
(#859) and v11 (#861) by widening the two models below, while the public
projection in Platform kept its own ``Literal[9]`` and 500'd on the first v10
score a carried-forward validator reported. Extend the alias when the evidence
contract reaches a new epoch, and every consumer moves with it.
"""

V9_EVIDENCE_BENCH_VERSIONS: tuple[int, ...] = get_args(V9EvidenceBenchVersion)
"""Every epoch whose reports carry the signed v9 base-evidence stack.

Derived from the alias, never restated. ``normalize_v9_score_report_omitempty``
above reads this: an enforced gate can zero both the composite and its stderr,
Go omits the zero stderr, and a report whose epoch is missing here therefore
fails ``ScoreReport`` validation instead of landing as the zero the gate found.
A hand-written ``(9, 10, 11)`` there is what stranded bench 12 in that state.
"""

CONFIRMATION_BENCH_VERSIONS: tuple[int, ...] = V9_EVIDENCE_BENCH_VERSIONS
"""Every epoch the confirmation lane can run on, derived from the alias above.

Derived, never restated. The LongMem confirmation lane projects the signed base
evidence, so "can this benchmark be confirmed" is not an independent policy --
it is exactly "does this benchmark carry the evidence stack". Writing that as a
second constant is what stranded the whole lane on bench 9 while the network ran
on 11: the alias had already been carried forward, and eight separate literals
had not.
"""

MIN_CONFIRMATION_BENCH_VERSION: int = min(CONFIRMATION_BENCH_VERSIONS)
"""Floor of the contract. Schema-level guards use this; policy uses membership."""


def supports_confirmation(bench_version: int | None) -> bool:
    """Whether ``bench_version`` carries the evidence the lane needs.

    Membership, not ``>= MIN``, so this fails closed: activating an epoch
    without carrying the evidence contract forward produces no confirmation
    work, rather than bundles whose base proof can never be parsed.
    """
    return bench_version in CONFIRMATION_BENCH_VERSIONS


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


class V12ModelDependenceGate(BaseModel):
    """Causal model-dependence gate. Present on every bench_version>=12 digest."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    administered_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    eligible_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    dependent_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    independent_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    slice_attribution_complete: bool
    dependence_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    threshold_bps: Annotated[int, Field(ge=1, le=_BASIS_POINTS)]
    result: V9GateResult
    factor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V12ModelDependenceGate:
        if self.eligible_cases > self.administered_cases:
            raise ValueError("dependence eligible cases exceed administered cases")
        if self.dependent_cases > self.eligible_cases:
            raise ValueError("dependence dependent cases exceed eligible cases")
        if self.independent_cases != self.eligible_cases - self.dependent_cases:
            raise ValueError("dependence independent cases are not derived correctly")
        if not self.slice_attribution_complete:
            # Fail OPEN: an unsettled counterfactual is a validator/relay gap, not
            # proof the answers are model-independent. Keep the signed
            # insufficient_evidence result and a full factor so an honest run is
            # never zeroed for missing ablation telemetry.
            dependence, result, factor = 0, "insufficient_evidence", _BASIS_POINTS
        elif self.eligible_cases == 0:
            dependence, result, factor = (
                _BASIS_POINTS,
                "not_applicable",
                _BASIS_POINTS,
            )
        else:
            dependence = _coverage_bps(self.dependent_cases, self.eligible_cases)
            if dependence < self.threshold_bps:
                result, factor = "below_threshold", 0
            else:
                result, factor = "passed", _BASIS_POINTS
        if (self.dependence_bps, self.result, self.factor_bps) != (
            dependence,
            result,
            factor,
        ):
            raise ValueError("model-dependence derived evidence is inconsistent")
        return self


class V12InferenceLatencyGate(BaseModel):
    """Inference-latency gate. Canonicalized only when administered."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    administered_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    eligible_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    flagged_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    unflagged_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    floor_ms: Annotated[int, Field(ge=1)]
    attribution_complete: bool
    posture: Literal["review", "enforce"]
    sub_floor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    threshold_bps: Annotated[int, Field(ge=1, le=_BASIS_POINTS)]
    result: V9GateResult
    factor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V12InferenceLatencyGate:
        if self.eligible_cases > self.administered_cases:
            raise ValueError("latency eligible cases exceed administered cases")
        if self.flagged_cases > self.eligible_cases:
            raise ValueError("latency flagged cases exceed eligible cases")
        if self.unflagged_cases != self.eligible_cases - self.flagged_cases:
            raise ValueError("latency unflagged cases are not derived correctly")
        if not self.attribution_complete:
            sub_floor, result, factor = 0, "insufficient_evidence", _BASIS_POINTS
        elif self.eligible_cases == 0:
            sub_floor, result, factor = 0, "not_applicable", _BASIS_POINTS
        else:
            sub_floor = _coverage_bps(self.flagged_cases, self.eligible_cases)
            if sub_floor < self.threshold_bps:
                result, factor = "passed", _BASIS_POINTS
            elif self.posture == "enforce":
                result, factor = "latency_implausible", 0
            else:
                result, factor = "latency_implausible", _BASIS_POINTS
        if (self.sub_floor_bps, self.result, self.factor_bps) != (
            sub_floor,
            result,
            factor,
        ):
            raise ValueError("inference-latency derived evidence is inconsistent")
        return self


class V12AnswerStuffingGate(BaseModel):
    """Answer-stuffing gate. Canonicalized only when administered."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    administered_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    eligible_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    stuffed_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    clean_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    attribution_complete: bool
    review_required: bool
    posture: Literal["enforce", "penalize", "review"]
    stuffed_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    threshold_bps: Annotated[int, Field(ge=1, le=_BASIS_POINTS)]
    min_cases: Annotated[int, Field(ge=1)]
    loose_eligible_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    loose_stuffed_cases: Annotated[int, Field(ge=0, le=_V9_MAX_COUNT)]
    loose_stuffed_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    review_share_threshold_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    result: V9GateResult
    factor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V12AnswerStuffingGate:
        if self.eligible_cases > self.administered_cases:
            raise ValueError("answer-stuffing eligible cases exceed administered cases")
        if self.stuffed_cases > self.eligible_cases:
            raise ValueError("answer-stuffing stuffed cases exceed eligible cases")
        if self.clean_cases != self.eligible_cases - self.stuffed_cases:
            raise ValueError("answer-stuffing clean cases are not derived correctly")
        if self.loose_eligible_cases > self.administered_cases:
            raise ValueError(
                "answer-stuffing loose eligible cases exceed administered cases"
            )
        if self.loose_stuffed_cases > self.loose_eligible_cases:
            raise ValueError(
                "answer-stuffing loose stuffed cases exceed loose eligible cases"
            )
        stuffed_bps = 0
        loose_stuffed_bps = 0
        if not self.attribution_complete:
            result, factor = "insufficient_evidence", _BASIS_POINTS
        else:
            if self.eligible_cases > 0:
                stuffed_bps = _coverage_bps(self.stuffed_cases, self.eligible_cases)
            if self.loose_eligible_cases > 0:
                loose_stuffed_bps = _coverage_bps(
                    self.loose_stuffed_cases, self.loose_eligible_cases
                )
            loose_review = (
                self.review_share_threshold_bps > 0
                and self.loose_stuffed_cases >= _ANSWER_STUFFING_REVIEW_MIN_CASES
                and loose_stuffed_bps >= self.review_share_threshold_bps
            )
            if self.review_required:
                result, factor = "review_required", _BASIS_POINTS
            elif self.eligible_cases > 0 and self.stuffed_cases >= self.min_cases:
                result = "answer_stuffed"
                if self.posture == "enforce":
                    factor = 0
                elif self.posture == "penalize":
                    penalty = min(stuffed_bps, _ANSWER_STUFFING_MAX_PENALTY_BPS)
                    factor = _BASIS_POINTS - penalty
                else:
                    factor = _BASIS_POINTS
            elif loose_review:
                result, factor = "review_required", _BASIS_POINTS
            elif self.eligible_cases == 0:
                result, factor = "not_applicable", _BASIS_POINTS
            else:
                result, factor = "passed", _BASIS_POINTS
        if (
            self.stuffed_bps,
            self.loose_stuffed_bps,
            self.result,
            self.factor_bps,
        ) != (stuffed_bps, loose_stuffed_bps, result, factor):
            raise ValueError("answer-stuffing derived evidence is inconsistent")
        return self


class V9ScoreGateEvidence(BaseModel):
    """Complete typed mirror of ``internal/scoregates.Evidence``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1]
    bench_version: V9EvidenceBenchVersion
    rollout_mode: Literal["shadow", "enforce"]
    threshold_profile: V9ThresholdProfile
    model_use: V9ModelUseGate
    authoritative_tool: V9AuthoritativeToolGate
    model_dependence: V12ModelDependenceGate | None = None
    inference_latency: V12InferenceLatencyGate | None = None
    answer_stuffing: V12AnswerStuffingGate | None = None

    @model_validator(mode="after")
    def _validate_versioned_gates(self) -> V9ScoreGateEvidence:
        v12 = self.bench_version >= 12
        if v12 and self.model_dependence is None:
            raise ValueError("v12 score gates require model_dependence")
        if not v12 and (
            self.model_dependence is not None
            or self.inference_latency is not None
            or self.answer_stuffing is not None
        ):
            raise ValueError("pre-v12 score gates must omit v12 gates")
        return self

    def combined_factor_bps(self) -> int:
        """Mirror Go ``Evidence.CombinedFactorBPS``."""
        if (
            self.model_use.factor_bps == 0
            or self.authoritative_tool.factor_bps == 0
        ):
            return 0
        if (
            self.bench_version >= 12
            and self.model_dependence is not None
            and self.model_dependence.factor_bps == 0
        ):
            return 0
        combined = _BASIS_POINTS
        if self.bench_version >= 12:
            if self.inference_latency is not None:
                combined = combined * self.inference_latency.factor_bps // _BASIS_POINTS
            if self.answer_stuffing is not None:
                combined = combined * self.answer_stuffing.factor_bps // _BASIS_POINTS
        return combined

    def canonical_bytes(self) -> bytes:
        model = self.model_use
        excluded = model.excluded
        tool = self.authoritative_tool
        body = (
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
        )
        if self.bench_version >= 12:
            dep = self.model_dependence
            assert dep is not None
            body += (
                f"model_dependence.administered_cases={dep.administered_cases}\n"
                f"model_dependence.eligible_cases={dep.eligible_cases}\n"
                f"model_dependence.dependent_cases={dep.dependent_cases}\n"
                f"model_dependence.independent_cases={dep.independent_cases}\n"
                "model_dependence.slice_attribution_complete="
                f"{_go_bool(dep.slice_attribution_complete)}\n"
                f"model_dependence.dependence_bps={dep.dependence_bps}\n"
                f"model_dependence.threshold_bps={dep.threshold_bps}\n"
                f"model_dependence.result={dep.result}\n"
                f"model_dependence.factor_bps={dep.factor_bps}\n"
            )
            lat = self.inference_latency
            if lat is not None:
                body += (
                    f"inference_latency.administered_cases={lat.administered_cases}\n"
                    f"inference_latency.eligible_cases={lat.eligible_cases}\n"
                    f"inference_latency.flagged_cases={lat.flagged_cases}\n"
                    f"inference_latency.unflagged_cases={lat.unflagged_cases}\n"
                    f"inference_latency.floor_ms={lat.floor_ms}\n"
                    "inference_latency.attribution_complete="
                    f"{_go_bool(lat.attribution_complete)}\n"
                    f"inference_latency.posture={lat.posture}\n"
                    f"inference_latency.sub_floor_bps={lat.sub_floor_bps}\n"
                    f"inference_latency.threshold_bps={lat.threshold_bps}\n"
                    f"inference_latency.result={lat.result}\n"
                    f"inference_latency.factor_bps={lat.factor_bps}\n"
                )
            stuffing = self.answer_stuffing
            if stuffing is not None:
                body += (
                    "answer_stuffing.administered_cases="
                    f"{stuffing.administered_cases}\n"
                    f"answer_stuffing.eligible_cases={stuffing.eligible_cases}\n"
                    f"answer_stuffing.stuffed_cases={stuffing.stuffed_cases}\n"
                    f"answer_stuffing.clean_cases={stuffing.clean_cases}\n"
                    "answer_stuffing.attribution_complete="
                    f"{_go_bool(stuffing.attribution_complete)}\n"
                    "answer_stuffing.review_required="
                    f"{_go_bool(stuffing.review_required)}\n"
                    f"answer_stuffing.posture={stuffing.posture}\n"
                    f"answer_stuffing.stuffed_bps={stuffing.stuffed_bps}\n"
                    f"answer_stuffing.threshold_bps={stuffing.threshold_bps}\n"
                    f"answer_stuffing.min_cases={stuffing.min_cases}\n"
                    "answer_stuffing.loose_eligible_cases="
                    f"{stuffing.loose_eligible_cases}\n"
                    "answer_stuffing.loose_stuffed_cases="
                    f"{stuffing.loose_stuffed_cases}\n"
                    f"answer_stuffing.loose_stuffed_bps={stuffing.loose_stuffed_bps}\n"
                    "answer_stuffing.review_share_threshold_bps="
                    f"{stuffing.review_share_threshold_bps}\n"
                    f"answer_stuffing.result={stuffing.result}\n"
                    f"answer_stuffing.factor_bps={stuffing.factor_bps}\n"
                )
        return body.encode()

    def digest_hex(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class V9BaseEvidence(BaseModel):
    """Signature-bound ordinary v9 score identity and binary gate evidence."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1]
    bench_version: V9EvidenceBenchVersion
    score_contract: V9ScoreContract
    run_id: Annotated[str, Field(min_length=1)]
    artifact_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    dataset_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    transcript_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    ordinary_composite_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    ordinary_stderr_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    score_gates: V9ScoreGateEvidence
    score_gates_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    semantic_gate_factor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    applied_gate_factor_bps: Annotated[int, Field(ge=0, le=_BASIS_POINTS)]
    effective_composite_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    effective_stderr_micros: Annotated[int, Field(ge=0, le=1_000_000)]

    @model_validator(mode="after")
    def _validate_derived_evidence(self) -> V9BaseEvidence:
        if self.bench_version != self.score_gates.bench_version:
            raise ValueError("score_gates bench_version does not match base evidence")
        if self.score_gates_sha256 != self.score_gates.digest_hex():
            raise ValueError("score_gates_sha256 does not match score_gates")
        semantic = self.score_gates.combined_factor_bps()
        applied = (
            _BASIS_POINTS if self.score_gates.rollout_mode == "shadow" else semantic
        )
        if self.semantic_gate_factor_bps != semantic:
            raise ValueError("semantic gate factor contradicts score-gate evidence")
        if self.applied_gate_factor_bps != applied:
            raise ValueError("applied gate factor contradicts rollout mode")
        expected_composite = _apply_gate_factor_micros(
            self.ordinary_composite_micros, applied
        )
        expected_stderr = _apply_gate_factor_micros(
            self.ordinary_stderr_micros, applied
        )
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

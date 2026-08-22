"""Canonical Bench v9 confirmation evidence shared by validator and Platform.

These Pydantic classes are the semantic and field-order authority for every
normalized confirmation dimension and signed evidence root. Public API modules
re-export them so existing import paths remain stable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ditto_screening_protocol.bench_v9 import V9EvidenceBenchVersion

Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]
ReceiptSetDigest = Annotated[
    str,
    StringConstraints(pattern=r"^(?:|[0-9a-f]{64})$", max_length=64),
]

ScoreMicros = Annotated[int, Field(ge=0, le=1_000_000)]
FactorBPS = Annotated[int, Field(ge=0, le=10_000)]
UsageCount = Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
PositiveUsageCount = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]

LongMemCapability = Literal[
    "extraction",
    "multi_session_reasoning",
    "temporal_reasoning",
    "knowledge_update",
    "preference",
    "abstention",
]

CAPABILITY_ORDER: tuple[LongMemCapability, ...] = (
    "extraction",
    "multi_session_reasoning",
    "temporal_reasoning",
    "knowledge_update",
    "preference",
    "abstention",
)


class LongMemCapabilityScore(BaseModel):
    """One member of the frozen six-capability LongMem macro average."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    capability: LongMemCapability
    correct: UsageCount
    count: PositiveUsageCount
    mean_micros: ScoreMicros

    @model_validator(mode="after")
    def _validate_counts(self) -> LongMemCapabilityScore:
        if self.correct > self.count:
            raise ValueError("correct cannot exceed count")
        return self


class LongMemProviderLaneEvidence(BaseModel):
    """Receipt-derived accounting for one frozen LongMem provider lane."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    lane: Annotated[str, Field(min_length=1, max_length=128)]
    cost_source: Literal["provider_receipt_v1"]
    currency: Literal["USD"]
    provider: Annotated[str, Field(min_length=1, max_length=128)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    fallback_used: Literal[False]
    requests: UsageCount
    successes: UsageCount
    receipted_requests: UsageCount
    prompt_tokens: UsageCount
    completion_tokens: UsageCount
    total_tokens: UsageCount
    cost_usd_micros: UsageCount
    receipt_set_sha256: ReceiptSetDigest

    @model_validator(mode="after")
    def _validate_accounting(self) -> LongMemProviderLaneEvidence:
        if self.requests == 0:
            if (
                self.successes != 0
                or self.receipted_requests != 0
                or self.prompt_tokens != 0
                or self.completion_tokens != 0
                or self.total_tokens != 0
                or self.cost_usd_micros != 0
                or self.receipt_set_sha256 != ""
            ):
                raise ValueError("zero provider lane must have exact zero accounting")
            return self
        if self.successes == 0 or self.receipted_requests == 0:
            raise ValueError(
                "positive provider lane must have successful receipted requests"
            )
        if len(self.receipt_set_sha256) != 64:
            raise ValueError("positive provider lane must have a receipt digest")
        if self.successes > self.requests:
            raise ValueError("successes cannot exceed requests")
        if self.receipted_requests != self.requests:
            raise ValueError("every provider request must have a receipt")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt plus completion tokens")
        return self


class LongMemScoreEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    longmem_mean_micros: ScoreMicros
    longmem_stderr_micros: ScoreMicros
    case_count: PositiveUsageCount
    per_capability: list[LongMemCapabilityScore]


class LongMemEvidence(BaseModel):
    """Typed mirror of the trusted LongMem evidence, with integer scores."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: Literal[2]
    artifact_sha256: Sha256
    bench_version: V9EvidenceBenchVersion
    profile_checksum: Sha256
    case_set_digest: Sha256
    dataset_revision: Annotated[str, Field(min_length=1, max_length=128)]
    dataset_sha256: Sha256
    score: LongMemScoreEvidence
    provider_evidence: list[LongMemProviderLaneEvidence]

    @model_validator(mode="after")
    def _validate_zero_provider_form(self) -> LongMemEvidence:
        zero_rows = [row.requests == 0 for row in self.provider_evidence]
        if any(zero_rows):
            exact_zero_score = (
                self.score.longmem_mean_micros == 0
                and self.score.longmem_stderr_micros == 0
                and all(
                    row.correct == 0 and row.mean_micros == 0
                    for row in self.score.per_capability
                )
            )
            if all(zero_rows):
                if not exact_zero_score:
                    raise ValueError(
                        "zero-provider LongMem evidence requires an exact zero score"
                    )
                return self
            lanes = {row.lane: row for row in self.provider_evidence}
            reader = lanes.get("reader")
            judge = lanes.get("judge")
            if (
                len(self.provider_evidence) != 2
                or len(lanes) != 2
                or reader is None
                or judge is None
                or reader.requests != 0
                or judge.requests != self.score.case_count
                or judge.successes != self.score.case_count
                or judge.receipted_requests != self.score.case_count
                or not exact_zero_score
            ):
                raise ValueError(
                    "mixed LongMem provider evidence requires an unused reader, "
                    "one receipted judge request per case, and an exact zero score"
                )
        return self


class AblationBudget(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    max_chat_requests: UsageCount
    max_chat_input_bytes: UsageCount
    max_embedding_requests: UsageCount
    max_embedding_inputs: UsageCount
    max_embedding_input_bytes: UsageCount


class AblationSyntheticUsage(BaseModel):
    """Provider-free intervention accounting; upstream use is always zero."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    synthetic: Literal[True]
    intervention: Literal["inference", "embedding"]
    budget: AblationBudget
    chat_attempts: UsageCount
    chat_applied: UsageCount
    chat_input_bytes: UsageCount
    embedding_attempts: UsageCount
    embedding_applied: UsageCount
    embedding_inputs: UsageCount
    embedding_input_bytes: UsageCount
    rejected_requests: UsageCount
    budget_exhausted: bool
    upstream_requests: Literal[0]
    upstream_input_tokens: Literal[0]
    upstream_output_tokens: Literal[0]
    upstream_provider_cost_microusd: Literal[0]

    @model_validator(mode="after")
    def _validate_synthetic_counts(self) -> AblationSyntheticUsage:
        if self.chat_applied > self.chat_attempts:
            raise ValueError("chat_applied cannot exceed chat_attempts")
        if self.embedding_applied > self.embedding_attempts:
            raise ValueError("embedding_applied cannot exceed embedding_attempts")
        if self.chat_applied > self.budget.max_chat_requests:
            raise ValueError("chat applications exceed the signed synthetic budget")
        if self.chat_input_bytes > self.budget.max_chat_input_bytes:
            raise ValueError("chat bytes exceed the signed synthetic budget")
        if self.embedding_applied > self.budget.max_embedding_requests:
            raise ValueError(
                "embedding applications exceed the signed synthetic budget"
            )
        if self.embedding_inputs > self.budget.max_embedding_inputs:
            raise ValueError("embedding inputs exceed the signed synthetic budget")
        if self.embedding_input_bytes > self.budget.max_embedding_input_bytes:
            raise ValueError("embedding bytes exceed the signed synthetic budget")
        if self.intervention == "inference":
            if any(
                (
                    self.embedding_attempts,
                    self.embedding_applied,
                    self.embedding_inputs,
                    self.embedding_input_bytes,
                )
            ):
                raise ValueError(
                    "inference ablation contains embedding synthetic usage"
                )
            attempts, applied = self.chat_attempts, self.chat_applied
        else:
            if any((self.chat_attempts, self.chat_applied, self.chat_input_bytes)):
                raise ValueError("embedding ablation contains chat usage")
            attempts, applied = self.embedding_attempts, self.embedding_applied
        if attempts != applied + self.rejected_requests:
            raise ValueError("synthetic attempt accounting is inconsistent")
        if (self.rejected_requests > 0) != self.budget_exhausted:
            raise ValueError("synthetic budget exhaustion accounting is inconsistent")
        return self


class AblationEvidence(BaseModel):
    """One binary semantic gate; it is never a weighted score dimension."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    contract_version: Annotated[str, Field(min_length=1, max_length=128)]
    bench_version: V9EvidenceBenchVersion
    artifact_sha256: Sha256
    intervention: Literal["inference", "embedding"]
    mode: Literal["off", "shadow", "enforce"]
    status: Literal["not_run", "passed", "failed", "unavailable"]
    reason: Annotated[str, Field(min_length=1)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    profile_checksum: Sha256
    threshold_manifest_sha256: Sha256
    coordinator_sha256: Sha256
    dataset_sha256: Sha256
    case_set_sha256: Sha256
    baseline_scores_sha256: Sha256 | None
    ablated_scores_sha256: Sha256 | None
    baseline_mean_micros: ScoreMicros | None
    ablated_mean_micros: ScoreMicros | None
    delta_micros: Annotated[int, Field(ge=-1_000_000, le=1_000_000)] | None
    threshold_micros: ScoreMicros
    sample_count: UsageCount
    affected_call_count: UsageCount
    semantic_factor_bps: FactorBPS | None
    applied_factor_bps: FactorBPS | None
    synthetic_usage: AblationSyntheticUsage

    @model_validator(mode="after")
    def _validate_result_shape(self) -> AblationEvidence:
        complete_values = (
            self.baseline_scores_sha256,
            self.ablated_scores_sha256,
            self.baseline_mean_micros,
            self.ablated_mean_micros,
            self.delta_micros,
            self.semantic_factor_bps,
            self.applied_factor_bps,
        )
        if self.intervention != self.synthetic_usage.intervention:
            raise ValueError("synthetic usage intervention does not match evidence")
        if self.status in {"passed", "failed"}:
            if any(value is None for value in complete_values):
                raise ValueError("completed ablation evidence is incomplete")
            assert self.baseline_mean_micros is not None
            assert self.ablated_mean_micros is not None
            assert self.delta_micros is not None
            if self.sample_count == 0:
                raise ValueError("completed ablation requires a non-empty sample")
            if self.delta_micros != (
                self.baseline_mean_micros - self.ablated_mean_micros
            ):
                raise ValueError("delta_micros does not match the paired means")
            drop_meets_threshold = self.delta_micros >= self.threshold_micros
            if self.reason == "observational_drop_not_causal":
                if self.status != "failed" or self.mode != "shadow":
                    raise ValueError(
                        "observational drop requires a completed shadow failure"
                    )
                if not drop_meets_threshold:
                    raise ValueError(
                        "observational drop requires a threshold-meeting delta"
                    )
            elif drop_meets_threshold != (self.status == "passed"):
                raise ValueError("ablation status contradicts its threshold")
            expected_semantic = 10_000 if self.status == "passed" else 0
            expected_applied = 10_000 if self.mode == "shadow" else expected_semantic
            if self.semantic_factor_bps != expected_semantic:
                raise ValueError("semantic factor contradicts ablation result")
            if self.applied_factor_bps != expected_applied:
                raise ValueError("applied factor contradicts rollout mode")
        elif any(value is not None for value in complete_values):
            raise ValueError(
                "not-run or unavailable ablation cannot carry a numeric gate"
            )
        if (
            self.status == "unavailable"
            and self.reason == "synthetic_budget_exhausted"
            and not self.synthetic_usage.budget_exhausted
        ):
            raise ValueError("budget-exhausted evidence lacks rejected usage")
        if self.reason == "counterfactual_proof_unavailable" and (
            self.status != "unavailable" or self.mode == "off"
        ):
            raise ValueError(
                "counterfactual proof reason requires an active unavailable result"
            )
        affected = (
            self.synthetic_usage.chat_applied
            if self.intervention == "inference"
            else self.synthetic_usage.embedding_applied
        )
        if self.affected_call_count != affected:
            raise ValueError("ablation affected-call count is not derived")
        if (
            self.status in {"passed", "failed"}
            and self.synthetic_usage.budget_exhausted
        ):
            raise ValueError("budget-exhausted ablation cannot complete")
        return self


class LongMemDimensionEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    status: Literal["completed"]
    evidence_sha256: Sha256
    latency_ms: UsageCount
    request_count: UsageCount
    input_tokens: UsageCount
    output_tokens: UsageCount
    provider_cost_microusd: UsageCount
    synthetic: Literal[False]
    evidence: LongMemEvidence


class AblationDimensionEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    status: Literal["completed", "not_run", "unavailable"]
    evidence_sha256: Sha256
    latency_ms: UsageCount
    request_count: Literal[0]
    input_tokens: Literal[0]
    output_tokens: Literal[0]
    provider_cost_microusd: Literal[0]
    synthetic: Literal[True]
    evidence: AblationEvidence

    @model_validator(mode="after")
    def _validate_status(self) -> AblationDimensionEnvelope:
        expected = {
            "passed": "completed",
            "failed": "completed",
            "not_run": "not_run",
            "unavailable": "unavailable",
        }[self.evidence.status]
        if self.status != expected:
            raise ValueError("dimension status contradicts ablation evidence")
        return self


class ConfirmationUsageTotals(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    request_count: UsageCount
    input_tokens: UsageCount
    output_tokens: UsageCount
    provider_cost_microusd: UsageCount
    latency_ms: UsageCount


class ConfirmationCompositePolicy(BaseModel):
    """Frozen arithmetic bound into the signed confirmation evidence root.

    The outer confirmation-profile checksum commits to this policy, but a
    ledger consumer cannot independently recover the policy from that digest.
    Carrying the exact policy in the signed root lets validators recompute a
    subject's full composite without trusting a Platform-projected scalar.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: Literal[1]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    formula_revision: Literal["weighted-quality-gates-v1"]
    base_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    longmem_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    checksum: Sha256

    @model_validator(mode="after")
    def _validate_weights(self) -> ConfirmationCompositePolicy:
        if self.base_weight_bps + self.longmem_weight_bps != 10_000:
            raise ValueError("confirmation composite weights must sum to 10000")
        return self


class ConfirmationCompletionReport(BaseModel):
    """Raw typed evidence plus exactly one signature over the rebuilt root."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    ablation_coordinator_latency_ms: UsageCount
    longmemeval: LongMemDimensionEnvelope
    inference_ablation: AblationDimensionEnvelope
    embedding_ablation: AblationDimensionEnvelope
    bundle_signature: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]+$", min_length=2, max_length=512),
    ]

    @model_validator(mode="after")
    def _validate_interventions(self) -> ConfirmationCompletionReport:
        if self.inference_ablation.evidence.intervention != "inference":
            raise ValueError("inference_ablation has the wrong intervention")
        if self.embedding_ablation.evidence.intervention != "embedding":
            raise ValueError("embedding_ablation has the wrong intervention")
        return self


class ConfirmationEvidenceRoot(BaseModel):
    """Server-rebuilt shared bundle evidence; never owns subject quality."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: Literal[1]
    artifact_sha256: Sha256
    bench_version: V9EvidenceBenchVersion
    confirmation_profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    confirmation_profile_checksum: Sha256
    settings_revision: Annotated[int, Field(ge=1)]
    settings_checksum: Sha256
    retest_generation: Annotated[int, Field(ge=0)]
    ablation_coordinator_latency_ms: UsageCount
    composite_policy: ConfirmationCompositePolicy
    longmemeval: LongMemDimensionEnvelope
    inference_ablation: AblationDimensionEnvelope
    embedding_ablation: AblationDimensionEnvelope
    totals: ConfirmationUsageTotals

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> ConfirmationEvidenceRoot:
        if self.inference_ablation.evidence.intervention != "inference":
            raise ValueError("inference_ablation has the wrong intervention")
        if self.embedding_ablation.evidence.intervention != "embedding":
            raise ValueError("embedding_ablation has the wrong intervention")
        expected_totals = ConfirmationUsageTotals(
            request_count=self.longmemeval.request_count,
            input_tokens=self.longmemeval.input_tokens,
            output_tokens=self.longmemeval.output_tokens,
            provider_cost_microusd=self.longmemeval.provider_cost_microusd,
            latency_ms=(
                self.longmemeval.latency_ms + self.ablation_coordinator_latency_ms
            ),
        )
        if self.totals != expected_totals:
            raise ValueError("confirmation totals are not derived from dimensions")
        return self


class V9ConfirmationCompositePolicy(ConfirmationCompositePolicy):
    """Schema-name-compatible shared policy used by public validator receipts."""

    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class V9ConfirmationEvidenceRoot(ConfirmationEvidenceRoot):
    """Schema-name-compatible shared root used by public validator receipts.

    The receipt contract predates the private confirmation transport and pins
    its SHA fields as pattern-only JSON Schema nodes.  Keep those public schema
    details while inheriting the canonical root field order and validators.
    """

    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    confirmation_profile_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    settings_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    composite_policy: V9ConfirmationCompositePolicy


CANONICAL_CONFIRMATION_ROOT_FIELDS = tuple(ConfirmationEvidenceRoot.model_fields)


def canonical_json(value: object) -> bytes:
    """Serialize typed confirmation evidence in its shared field order."""
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def evidence_digest(value: object) -> str:
    """Return the SHA-256 of shared canonical confirmation JSON."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


__all__ = [
    "AblationBudget",
    "AblationDimensionEnvelope",
    "AblationEvidence",
    "AblationSyntheticUsage",
    "CAPABILITY_ORDER",
    "CANONICAL_CONFIRMATION_ROOT_FIELDS",
    "ConfirmationCompletionReport",
    "ConfirmationCompositePolicy",
    "ConfirmationEvidenceRoot",
    "ConfirmationUsageTotals",
    "FactorBPS",
    "LongMemCapability",
    "LongMemCapabilityScore",
    "LongMemDimensionEnvelope",
    "LongMemEvidence",
    "LongMemProviderLaneEvidence",
    "LongMemScoreEvidence",
    "PositiveUsageCount",
    "ScoreMicros",
    "Sha256",
    "UsageCount",
    "V9ConfirmationCompositePolicy",
    "V9ConfirmationEvidenceRoot",
    "canonical_json",
    "evidence_digest",
]

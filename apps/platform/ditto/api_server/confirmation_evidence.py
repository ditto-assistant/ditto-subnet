"""Server-authoritative verification for Bench v9 confirmation evidence.

The validator supplies typed observations and one signature.  This module
rebuilds every nested digest, validates the frozen launch profile and accounting,
and computes per-subject projections.  Bundle evidence is deliberately shared;
quality and uncertainty never are.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ditto.api_models.confirmation_bundles import (
    AblationBudget,
    AblationDimensionEnvelope,
    ConfirmationBundleMode,
    ConfirmationCompletionReport,
    ConfirmationCompositePolicy,
    ConfirmationEvidenceRoot,
    ConfirmationUsageTotals,
    LongMemDimensionEnvelope,
)
from ditto_screening_protocol.confirmation import (
    CAPABILITY_ORDER,
    canonical_json,
    evidence_digest,
)

SCORE_SCALE = 1_000_000
FACTOR_SCALE = 10_000
LONGMEM_SELECTOR_REVISION_V1 = "longmemeval-s-stratified-sha256-v1"
ABLATION_PROFILE_CONTRACT_VERSION = "dittobench-v9-ablation-profile-v1"


class ConfirmationEvidenceError(ValueError):
    """Typed confirmation evidence contradicts its frozen server contract."""


def confirmation_inference_cap_requirements(
    profile: ConfirmationVerificationProfile,
) -> tuple[int, int]:
    """Return the full chat plus embedding request and token maxima."""
    requests = profile.embedding_lane.max_requests + sum(
        lane.max_requests for lane in profile.provider_lanes
    )
    tokens = profile.embedding_lane.max_input_tokens + sum(
        lane.max_total_tokens for lane in profile.provider_lanes
    )
    return requests, tokens


def validate_confirmation_inference_caps(
    profile: ConfirmationVerificationProfile,
    *,
    request_cap: int,
    token_cap: int,
) -> None:
    required_requests, required_tokens = confirmation_inference_cap_requirements(
        profile
    )
    if request_cap < required_requests or token_cap < required_tokens:
        raise ConfirmationEvidenceError(
            "confirmation policy cannot fund the frozen inference lane maxima"
        )


@dataclass(frozen=True)
class ProviderLanePolicy:
    lane: str
    provider: str
    route_provider: str
    receipt_provider: str
    profile_revision: str
    model: str
    max_requests: int
    max_prompt_tokens: int
    max_completion_tokens: int
    max_total_tokens: int
    max_cost_usd_micros: int

    def payload(self) -> dict[str, str | int]:
        return {
            "lane": self.lane,
            "provider": self.provider,
            "route_provider": self.route_provider,
            "receipt_provider": self.receipt_provider,
            "profile_revision": self.profile_revision,
            "model": self.model,
            "max_requests": self.max_requests,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_completion_tokens": self.max_completion_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd_micros": self.max_cost_usd_micros,
        }

    def longmem_profile_payload(self) -> dict[str, str | int]:
        """Return the stable research-profile shape consumed by Go.

        Platform route and receipt identities are transport authority. They
        are bound by the outer execution-profile checksum, while the existing
        LongMem research checksum intentionally remains byte compatible with
        the evaluator's provider-policy contract.
        """
        payload = self.payload()
        payload.pop("route_provider")
        payload.pop("receipt_provider")
        return payload


@dataclass(frozen=True)
class EmbeddingLanePolicy:
    lane: str
    provider: str
    profile_revision: str
    model: str
    dimensions: int
    max_requests: int
    max_input_tokens: int
    max_cost_usd_micros: int

    def payload(self) -> dict[str, str | int]:
        return {
            "lane": self.lane,
            "provider": self.provider,
            "profile_revision": self.profile_revision,
            "model": self.model,
            "dimensions": self.dimensions,
            "max_requests": self.max_requests,
            "max_input_tokens": self.max_input_tokens,
            "max_cost_usd_micros": self.max_cost_usd_micros,
        }


@dataclass(frozen=True)
class SyntheticBudgetPolicy:
    max_chat_requests: int
    max_chat_input_bytes: int
    max_embedding_requests: int
    max_embedding_inputs: int
    max_embedding_input_bytes: int

    def payload(self) -> dict[str, int]:
        return {
            "max_chat_requests": self.max_chat_requests,
            "max_chat_input_bytes": self.max_chat_input_bytes,
            "max_embedding_requests": self.max_embedding_requests,
            "max_embedding_inputs": self.max_embedding_inputs,
            "max_embedding_input_bytes": self.max_embedding_input_bytes,
        }

    def wire(self) -> AblationBudget:
        return AblationBudget.model_validate(self.payload())

    def compact_payload(self) -> dict[str, int]:
        """Mirror Go's ``omitempty`` JSON used by the frozen profile digest."""
        return {key: value for key, value in self.payload().items() if value != 0}


@dataclass(frozen=True)
class AblationCoordinatorPolicy:
    sample_size: int
    max_attempts: int
    max_requests: int
    request_timeout_milliseconds: int
    total_timeout_milliseconds: int

    def payload(self) -> dict[str, int]:
        return {
            "sample_size": self.sample_size,
            "max_attempts": self.max_attempts,
            "max_requests": self.max_requests,
            "request_timeout_milliseconds": self.request_timeout_milliseconds,
            "total_timeout_milliseconds": self.total_timeout_milliseconds,
        }


@dataclass(frozen=True)
class AblationVerificationPolicy:
    intervention: str
    contract_version: str
    threshold_micros: int
    budget: SyntheticBudgetPolicy

    def payload(self) -> dict[str, object]:
        return {
            "intervention": self.intervention,
            "contract_version": self.contract_version,
            "threshold_micros": self.threshold_micros,
            "budget": self.budget.payload(),
        }


@dataclass(frozen=True)
class CompositeVerificationPolicy:
    schema_version: int
    revision: str
    formula_revision: str
    base_weight_bps: int
    longmem_weight_bps: int

    def payload_without_checksum(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "formula_revision": self.formula_revision,
            "base_weight_bps": self.base_weight_bps,
            "longmem_weight_bps": self.longmem_weight_bps,
        }

    def checksum(self) -> str:
        return _digest(self.payload_without_checksum())

    def payload(self) -> dict[str, str | int]:
        return {**self.payload_without_checksum(), "checksum": self.checksum()}


@dataclass(frozen=True)
class ConfirmationVerificationProfile:
    schema_version: int
    revision: str
    longmem_profile_revision: str
    longmem_profile_checksum: str
    longmem_dataset_revision: str
    longmem_dataset_sha256: str
    longmem_selector_revision: str
    longmem_selection_seed: int
    longmem_cases_per_capability: int
    longmem_seed_batch_pairs: int
    longmem_projection_key_sha256: str
    provider_lanes: tuple[ProviderLanePolicy, ...]
    embedding_lane: EmbeddingLanePolicy
    ablation_profile_revision: str
    ablation_profile_checksum: str
    ablation_dataset_sha256: str
    ablation_threshold_manifest_sha256: str
    ablation_selection_key_sha256: str
    ablation_projection_key_sha256: str
    ablation_coordinator_policy: AblationCoordinatorPolicy
    inference_ablation: AblationVerificationPolicy
    embedding_ablation: AblationVerificationPolicy
    composite: CompositeVerificationPolicy

    def payload(self) -> dict[str, object]:
        lanes = sorted(self.provider_lanes, key=lambda lane: lane.lane)
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "longmem_profile_revision": self.longmem_profile_revision,
            "longmem_profile_checksum": self.longmem_profile_checksum,
            "longmem_dataset_revision": self.longmem_dataset_revision,
            "longmem_dataset_sha256": self.longmem_dataset_sha256,
            "longmem_selector_revision": self.longmem_selector_revision,
            "longmem_selection_seed": self.longmem_selection_seed,
            "longmem_cases_per_capability": self.longmem_cases_per_capability,
            "longmem_seed_batch_pairs": self.longmem_seed_batch_pairs,
            "longmem_projection_key_sha256": self.longmem_projection_key_sha256,
            "provider_lanes": [lane.payload() for lane in lanes],
            "embedding_lane": self.embedding_lane.payload(),
            "ablation_profile_revision": self.ablation_profile_revision,
            "ablation_profile_checksum": self.ablation_profile_checksum,
            "ablation_dataset_sha256": self.ablation_dataset_sha256,
            "ablation_threshold_manifest_sha256": (
                self.ablation_threshold_manifest_sha256
            ),
            "ablation_selection_key_sha256": self.ablation_selection_key_sha256,
            "ablation_projection_key_sha256": self.ablation_projection_key_sha256,
            "ablation_coordinator_policy": self.ablation_coordinator_policy.payload(),
            "inference_ablation": self.inference_ablation.payload(),
            "embedding_ablation": self.embedding_ablation.payload(),
            "composite": self.composite.payload(),
        }

    def checksum(self) -> str:
        self.validate()
        return _digest(self.payload())

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ConfirmationEvidenceError("confirmation profile schema must be 1")
        _require_text(self.revision, "confirmation profile revision")
        _require_text(self.longmem_profile_revision, "LongMem profile revision")
        _require_sha256(self.longmem_profile_checksum, "LongMem profile checksum")
        _require_text(self.longmem_dataset_revision, "LongMem dataset revision")
        _require_sha256(self.longmem_dataset_sha256, "LongMem dataset checksum")
        if self.longmem_selector_revision != LONGMEM_SELECTOR_REVISION_V1:
            raise ConfirmationEvidenceError("LongMem selector revision is unsupported")
        if not 0 <= self.longmem_selection_seed <= (1 << 64) - 1:
            raise ConfirmationEvidenceError("LongMem selection seed is out of range")
        if self.longmem_cases_per_capability < 2:
            raise ConfirmationEvidenceError(
                "LongMem cases per capability must preserve uncertainty"
            )
        if self.longmem_seed_batch_pairs <= 0:
            raise ConfirmationEvidenceError("LongMem seed batch pairs must be positive")
        _require_sha256(
            self.longmem_projection_key_sha256,
            "LongMem projection key checksum",
        )
        if not self.provider_lanes:
            raise ConfirmationEvidenceError("LongMem provider lanes are unconfigured")
        names: set[str] = set()
        for lane in self.provider_lanes:
            _validate_provider_policy(lane)
            if lane.lane in names:
                raise ConfirmationEvidenceError(
                    f"duplicate frozen provider lane {lane.lane!r}"
                )
            names.add(lane.lane)
        if self.longmem_profile_checksum != self.longmem_checksum():
            raise ConfirmationEvidenceError(
                "LongMem profile checksum does not bind its execution policy"
            )
        _validate_embedding_policy(self.embedding_lane)
        _require_text(self.ablation_profile_revision, "ablation profile revision")
        for value, label in (
            (self.ablation_profile_checksum, "ablation profile checksum"),
            (self.ablation_dataset_sha256, "ablation dataset checksum"),
            (
                self.ablation_threshold_manifest_sha256,
                "ablation threshold manifest",
            ),
            (self.ablation_selection_key_sha256, "ablation selection key checksum"),
            (
                self.ablation_projection_key_sha256,
                "ablation projection key checksum",
            ),
        ):
            _require_sha256(value, label)
        _validate_ablation_coordinator_policy(self.ablation_coordinator_policy)
        _validate_ablation_policy(self.inference_ablation, "inference")
        _validate_ablation_policy(self.embedding_ablation, "embedding")
        if self.ablation_profile_checksum != self.ablation_checksum():
            raise ConfirmationEvidenceError(
                "ablation profile checksum does not bind its execution policy"
            )
        composite = self.composite
        if composite.schema_version != 1:
            raise ConfirmationEvidenceError("composite profile schema must be 1")
        _require_text(composite.revision, "composite profile revision")
        _require_text(composite.formula_revision, "composite formula revision")
        if composite.formula_revision != "weighted-quality-gates-v1":
            raise ConfirmationEvidenceError(
                "confirmation composite formula revision is unsupported"
            )
        if composite.base_weight_bps <= 0 or composite.longmem_weight_bps <= 0:
            raise ConfirmationEvidenceError("composite weights must be positive")
        if composite.base_weight_bps + composite.longmem_weight_bps != FACTOR_SCALE:
            raise ConfirmationEvidenceError("composite weights must sum to 10000")

    def longmem_checksum(self) -> str:
        lanes = sorted(self.provider_lanes, key=lambda lane: lane.lane)
        return _digest(
            {
                "schema_version": 1,
                "revision": self.longmem_profile_revision,
                "bench_version": 9,
                "dataset_revision": self.longmem_dataset_revision,
                "dataset_sha256": self.longmem_dataset_sha256,
                "selector_revision": self.longmem_selector_revision,
                "selection_seed": self.longmem_selection_seed,
                "cases_per_capability": self.longmem_cases_per_capability,
                "providers": [lane.longmem_profile_payload() for lane in lanes],
            }
        )

    def ablation_checksum(self) -> str:
        return _go_ablation_profile_digest(
            (
                ("contract_version", ABLATION_PROFILE_CONTRACT_VERSION),
                ("revision", self.ablation_profile_revision),
                ("bench_version", 9),
                ("dataset_sha256", self.ablation_dataset_sha256),
                (
                    "threshold_manifest_sha256",
                    self.ablation_threshold_manifest_sha256,
                ),
                ("selection_key_sha256", self.ablation_selection_key_sha256),
                ("projection_key_sha256", self.ablation_projection_key_sha256),
                (
                    "coordinator_policy",
                    self.ablation_coordinator_policy.payload(),
                ),
                (
                    "inference_budget",
                    self.inference_ablation.budget.compact_payload(),
                ),
                (
                    "embedding_budget",
                    self.embedding_ablation.budget.compact_payload(),
                ),
                ("inference_threshold", self.inference_ablation.threshold_micros),
                ("embedding_threshold", self.embedding_ablation.threshold_micros),
            )
        )


@dataclass(frozen=True)
class VerifiedConfirmationEvidence:
    root: ConfirmationEvidenceRoot
    evidence_sha256: str
    longmem_mean_micros: int
    longmem_stderr_micros: int
    ablations_complete: bool
    ablation_semantic_factor_bps: int | None


@dataclass(frozen=True)
class SubjectProjection:
    result_status: str
    full_quality_micros: int | None
    full_stderr_micros: int | None
    semantic_factor_bps: int | None
    applied_factor_bps: int | None
    full_effective_micros: int | None


def confirmation_signing_message(
    *,
    reporter_hotkey: str,
    bundle_id: object,
    ticket_id: object,
    deadline: object,
    artifact_sha256: str,
    profile_revision: str,
    profile_checksum: str,
    settings_revision: int,
    settings_checksum: str,
    retest_generation: int,
    evidence_sha256: str,
) -> bytes:
    from datetime import UTC, datetime

    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise ConfirmationEvidenceError("ticket deadline must be timezone-aware")
    rendered_deadline = (
        deadline.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation:v1:"
        f"{reporter_hotkey}:{bundle_id}:{ticket_id}:{rendered_deadline}:"
        f"{artifact_sha256}:9:{profile_revision}:{profile_checksum}:"
        f"{settings_revision}:{settings_checksum}:{retest_generation}:"
        f"{evidence_sha256}"
    ).encode()


def rebuild_confirmation_evidence(
    report: ConfirmationCompletionReport,
    *,
    artifact_sha256: str,
    profile_revision: str,
    profile_checksum: str,
    settings_revision: int,
    settings_checksum: str,
    retest_generation: int,
    mode: ConfirmationBundleMode,
    profile: ConfirmationVerificationProfile,
) -> VerifiedConfirmationEvidence:
    """Replay every derived field and return the canonical shared root."""
    profile.validate()
    if profile.revision != profile_revision or profile.checksum() != profile_checksum:
        raise ConfirmationEvidenceError("confirmation profile identity drift")
    _require_sha256(settings_checksum, "settings checksum")
    longmem = _validate_longmem(
        report.longmemeval, artifact_sha256=artifact_sha256, profile=profile
    )
    inference = _validate_ablation(
        report.inference_ablation,
        artifact_sha256=artifact_sha256,
        mode=mode,
        profile=profile,
        policy=profile.inference_ablation,
    )
    embedding = _validate_ablation(
        report.embedding_ablation,
        artifact_sha256=artifact_sha256,
        mode=mode,
        profile=profile,
        policy=profile.embedding_ablation,
    )
    if inference.evidence.coordinator_sha256 != embedding.evidence.coordinator_sha256:
        raise ConfirmationEvidenceError(
            "ablation interventions do not share one run coordinator"
        )
    totals = ConfirmationUsageTotals(
        request_count=longmem.request_count,
        input_tokens=longmem.input_tokens,
        output_tokens=longmem.output_tokens,
        provider_cost_microusd=longmem.provider_cost_microusd,
        latency_ms=(longmem.latency_ms + report.ablation_coordinator_latency_ms),
    )
    root = ConfirmationEvidenceRoot(
        schema_version=1,
        artifact_sha256=artifact_sha256,
        bench_version=9,
        confirmation_profile_revision=profile_revision,
        confirmation_profile_checksum=profile_checksum,
        settings_revision=settings_revision,
        settings_checksum=settings_checksum,
        retest_generation=retest_generation,
        ablation_coordinator_latency_ms=report.ablation_coordinator_latency_ms,
        composite_policy=ConfirmationCompositePolicy.model_validate(
            profile.composite.payload()
        ),
        longmemeval=longmem,
        inference_ablation=inference,
        embedding_ablation=embedding,
        totals=totals,
    )
    complete = inference.status == "completed" and embedding.status == "completed"
    semantic = None
    if complete:
        semantic = _binary_product(
            inference.evidence.semantic_factor_bps,
            embedding.evidence.semantic_factor_bps,
        )
    return VerifiedConfirmationEvidence(
        root=root,
        evidence_sha256=evidence_digest(root),
        longmem_mean_micros=longmem.evidence.score.longmem_mean_micros,
        longmem_stderr_micros=longmem.evidence.score.longmem_stderr_micros,
        ablations_complete=complete,
        ablation_semantic_factor_bps=semantic,
    )


def compute_subject_projection(
    *,
    mode: ConfirmationBundleMode,
    base_quality_micros: int,
    base_stderr_micros: int,
    base_model_factor_bps: int,
    base_tool_factor_bps: int,
    verified: VerifiedConfirmationEvidence,
    composite: CompositeVerificationPolicy,
) -> SubjectProjection:
    """Compute one subject's full quality/effective/uncertainty server-side."""
    for name, value, maximum in (
        ("base quality", base_quality_micros, SCORE_SCALE),
        ("base stderr", base_stderr_micros, SCORE_SCALE),
        ("model factor", base_model_factor_bps, FACTOR_SCALE),
        ("tool factor", base_tool_factor_bps, FACTOR_SCALE),
    ):
        if not 0 <= value <= maximum:
            raise ConfirmationEvidenceError(f"{name} is out of range")
    if base_model_factor_bps not in {0, FACTOR_SCALE} or base_tool_factor_bps not in {
        0,
        FACTOR_SCALE,
    }:
        raise ConfirmationEvidenceError("base gates must be binary")
    if not verified.ablations_complete:
        return SubjectProjection("provisional", None, None, None, None, None)
    assert verified.ablation_semantic_factor_bps is not None
    semantic = _binary_product(
        base_model_factor_bps,
        base_tool_factor_bps,
        verified.ablation_semantic_factor_bps,
    )
    applied = FACTOR_SCALE if mode == ConfirmationBundleMode.SHADOW else semantic
    quality = _round_ratio(
        composite.base_weight_bps * base_quality_micros
        + composite.longmem_weight_bps * verified.longmem_mean_micros,
        FACTOR_SCALE,
    )
    with localcontext() as context:
        context.prec = 50
        base_component = (
            Decimal(composite.base_weight_bps)
            * Decimal(base_stderr_micros)
            / Decimal(FACTOR_SCALE)
        )
        longmem_component = (
            Decimal(composite.longmem_weight_bps)
            * Decimal(verified.longmem_stderr_micros)
            / Decimal(FACTOR_SCALE)
        )
        stderr = int(
            (base_component**2 + longmem_component**2)
            .sqrt()
            .quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
    effective = _round_ratio(quality * applied, FACTOR_SCALE)
    applied_stderr = _round_ratio(stderr * applied, FACTOR_SCALE)
    return SubjectProjection(
        result_status=(
            "full_confirmed"
            if mode == ConfirmationBundleMode.ENFORCE
            else "provisional"
        ),
        full_quality_micros=quality,
        full_stderr_micros=applied_stderr,
        semantic_factor_bps=semantic,
        applied_factor_bps=applied,
        full_effective_micros=effective,
    )


def _validate_longmem(
    envelope: LongMemDimensionEnvelope,
    *,
    artifact_sha256: str,
    profile: ConfirmationVerificationProfile,
) -> LongMemDimensionEnvelope:
    evidence = envelope.evidence
    if evidence.artifact_sha256 != artifact_sha256:
        raise ConfirmationEvidenceError("LongMem artifact digest drift")
    if evidence.profile_checksum != profile.longmem_profile_checksum:
        raise ConfirmationEvidenceError("LongMem profile checksum drift")
    if (
        evidence.dataset_revision != profile.longmem_dataset_revision
        or evidence.dataset_sha256 != profile.longmem_dataset_sha256
    ):
        raise ConfirmationEvidenceError("LongMem dataset identity drift")
    capabilities = evidence.score.per_capability
    if tuple(row.capability for row in capabilities) != CAPABILITY_ORDER:
        raise ConfirmationEvidenceError(
            "LongMem capabilities must appear once in frozen order"
        )
    total_cases = 0
    macro_sum = Decimal(0)
    macro_variance = Decimal(0)
    with localcontext() as context:
        context.prec = 60
        strata = Decimal(len(CAPABILITY_ORDER))
        for capability_row in capabilities:
            if capability_row.count < 2:
                raise ConfirmationEvidenceError(
                    f"LongMem capability {capability_row.capability!r} needs two cases"
                )
            expected_mean = _round_ratio(
                capability_row.correct * SCORE_SCALE, capability_row.count
            )
            if capability_row.mean_micros != expected_mean:
                raise ConfirmationEvidenceError(
                    "LongMem capability "
                    f"{capability_row.capability!r} mean is not derived"
                )
            count = Decimal(capability_row.count)
            mean = Decimal(capability_row.correct) / count
            macro_sum += mean / strata
            sample_variance = mean * (Decimal(1) - mean) * count / (count - 1)
            macro_variance += (sample_variance / count) / (strata * strata)
            total_cases += capability_row.count
        expected_macro = _round_decimal(macro_sum * SCORE_SCALE)
        expected_stderr = _round_decimal(macro_variance.sqrt() * SCORE_SCALE)
    if evidence.score.case_count != total_cases:
        raise ConfirmationEvidenceError("LongMem case count is not derived")
    if evidence.score.longmem_mean_micros != expected_macro:
        raise ConfirmationEvidenceError("LongMem macro mean is not derived")
    if evidence.score.longmem_stderr_micros != expected_stderr:
        raise ConfirmationEvidenceError("LongMem stderr is not derived")

    lanes = sorted(evidence.provider_evidence, key=lambda row: row.lane)
    policies = tuple(sorted(profile.provider_lanes, key=lambda row: row.lane))
    if tuple(row.lane for row in lanes) != tuple(row.lane for row in policies):
        raise ConfirmationEvidenceError(
            "LongMem provider evidence must cover every frozen lane exactly once"
        )
    for lane_row, policy in zip(lanes, policies, strict=True):
        if (
            lane_row.provider != policy.provider
            or lane_row.profile_revision != policy.profile_revision
            or lane_row.model != policy.model
        ):
            raise ConfirmationEvidenceError(
                f"LongMem provider identity drift on lane {policy.lane!r}"
            )
        if (
            lane_row.requests > policy.max_requests
            or lane_row.prompt_tokens > policy.max_prompt_tokens
            or lane_row.completion_tokens > policy.max_completion_tokens
            or lane_row.total_tokens > policy.max_total_tokens
            or lane_row.cost_usd_micros > policy.max_cost_usd_micros
        ):
            raise ConfirmationEvidenceError(
                f"LongMem provider lane {policy.lane!r} exceeds its frozen cap"
            )
    normalized_evidence = evidence.model_copy(update={"provider_evidence": lanes})
    if envelope.evidence_sha256 != evidence_digest(normalized_evidence):
        raise ConfirmationEvidenceError("LongMem evidence digest mismatch")
    requests = sum(row.requests for row in lanes)
    prompt_tokens = sum(row.prompt_tokens for row in lanes)
    completion_tokens = sum(row.completion_tokens for row in lanes)
    cost = sum(row.cost_usd_micros for row in lanes)
    if (
        envelope.request_count != requests
        or envelope.input_tokens != prompt_tokens
        or envelope.output_tokens != completion_tokens
        or envelope.provider_cost_microusd != cost
    ):
        raise ConfirmationEvidenceError(
            "LongMem envelope accounting does not equal its provider lanes"
        )
    return envelope.model_copy(update={"evidence": normalized_evidence})


def _validate_ablation(
    envelope: AblationDimensionEnvelope,
    *,
    artifact_sha256: str,
    mode: ConfirmationBundleMode,
    profile: ConfirmationVerificationProfile,
    policy: AblationVerificationPolicy,
) -> AblationDimensionEnvelope:
    evidence = envelope.evidence
    if evidence.artifact_sha256 != artifact_sha256:
        raise ConfirmationEvidenceError("ablation artifact digest drift")
    if evidence.mode != mode.value:
        raise ConfirmationEvidenceError("ablation mode does not match frozen settings")
    expected = (
        evidence.contract_version == policy.contract_version
        and evidence.intervention == policy.intervention
        and evidence.profile_revision == profile.ablation_profile_revision
        and evidence.profile_checksum == profile.ablation_profile_checksum
        and evidence.threshold_manifest_sha256
        == profile.ablation_threshold_manifest_sha256
        and evidence.dataset_sha256 == profile.ablation_dataset_sha256
        and evidence.threshold_micros == policy.threshold_micros
        and evidence.synthetic_usage.budget == policy.budget.wire()
    )
    if not expected:
        raise ConfirmationEvidenceError(f"{policy.intervention} ablation profile drift")
    usage = evidence.synthetic_usage
    if policy.intervention == "inference":
        if any(
            (
                usage.embedding_attempts,
                usage.embedding_applied,
                usage.embedding_inputs,
                usage.embedding_input_bytes,
            )
        ):
            raise ConfirmationEvidenceError(
                "inference ablation contains embedding synthetic usage"
            )
        if usage.chat_attempts != usage.chat_applied + usage.rejected_requests:
            raise ConfirmationEvidenceError(
                "inference synthetic attempt accounting is inconsistent"
            )
        affected = usage.chat_applied
    else:
        if any((usage.chat_attempts, usage.chat_applied, usage.chat_input_bytes)):
            raise ConfirmationEvidenceError(
                "embedding ablation contains chat synthetic usage"
            )
        if usage.embedding_attempts != (
            usage.embedding_applied + usage.rejected_requests
        ):
            raise ConfirmationEvidenceError(
                "embedding synthetic attempt accounting is inconsistent"
            )
        affected = usage.embedding_applied
    if (usage.rejected_requests > 0) != usage.budget_exhausted:
        raise ConfirmationEvidenceError(
            "synthetic budget exhaustion accounting is inconsistent"
        )
    if evidence.affected_call_count != affected:
        raise ConfirmationEvidenceError("ablation affected-call count is not derived")
    if evidence.status in {"passed", "failed"} and usage.budget_exhausted:
        raise ConfirmationEvidenceError("budget-exhausted ablation cannot complete")
    if (
        evidence.status == "unavailable"
        and evidence.reason == "synthetic_budget_exhausted"
        and not usage.budget_exhausted
    ):
        raise ConfirmationEvidenceError("budget exhaustion lacks rejected usage")
    if envelope.evidence_sha256 != evidence_digest(evidence):
        raise ConfirmationEvidenceError(
            f"{policy.intervention} ablation evidence digest mismatch"
        )
    return envelope


def _validate_provider_policy(policy: ProviderLanePolicy) -> None:
    for value, label in (
        (policy.lane, "provider lane"),
        (policy.provider, "provider"),
        (policy.route_provider, "route provider"),
        (policy.receipt_provider, "receipt provider"),
        (policy.profile_revision, "provider profile revision"),
        (policy.model, "provider model"),
    ):
        _require_text(value, label)
    caps = (
        policy.max_requests,
        policy.max_prompt_tokens,
        policy.max_completion_tokens,
        policy.max_total_tokens,
        policy.max_cost_usd_micros,
    )
    if any(value <= 0 for value in caps):
        raise ConfirmationEvidenceError(
            f"provider lane {policy.lane!r} must have positive caps"
        )
    if (
        policy.max_prompt_tokens > policy.max_total_tokens
        or policy.max_completion_tokens > policy.max_total_tokens
    ):
        raise ConfirmationEvidenceError(
            f"provider lane {policy.lane!r} token cap is inconsistent"
        )


def _validate_embedding_policy(policy: EmbeddingLanePolicy) -> None:
    for value, label in (
        (policy.lane, "embedding lane"),
        (policy.provider, "embedding provider"),
        (policy.profile_revision, "embedding profile revision"),
        (policy.model, "embedding model"),
    ):
        _require_text(value, label)
    if policy.lane != "embedding":
        raise ConfirmationEvidenceError("embedding lane identity is invalid")
    if not 0 < policy.dimensions <= 16_384:
        raise ConfirmationEvidenceError("embedding dimensions are invalid")
    if (
        policy.max_requests <= 0
        or policy.max_input_tokens <= 0
        or policy.max_cost_usd_micros <= 0
    ):
        raise ConfirmationEvidenceError("embedding lane caps are invalid")


def _validate_ablation_policy(
    policy: AblationVerificationPolicy, intervention: str
) -> None:
    if policy.intervention != intervention:
        raise ConfirmationEvidenceError(f"wrong {intervention} ablation policy")
    for value, label in ((policy.contract_version, "ablation contract"),):
        _require_text(value, label)
    if not 0 <= policy.threshold_micros <= SCORE_SCALE:
        raise ConfirmationEvidenceError("ablation threshold is out of range")
    try:
        policy.budget.wire()
    except ValueError as error:
        raise ConfirmationEvidenceError("invalid synthetic ablation budget") from error
    budget = policy.budget
    if intervention == "inference":
        if budget.max_chat_requests <= 0 or budget.max_chat_input_bytes <= 0:
            raise ConfirmationEvidenceError("inference budget must be positive")
        if any(
            (
                budget.max_embedding_requests,
                budget.max_embedding_inputs,
                budget.max_embedding_input_bytes,
            )
        ):
            raise ConfirmationEvidenceError(
                "inference budget contains embedding limits"
            )
    else:
        if (
            budget.max_embedding_requests <= 0
            or budget.max_embedding_inputs <= 0
            or budget.max_embedding_input_bytes <= 0
        ):
            raise ConfirmationEvidenceError("embedding budget must be positive")
        if budget.max_chat_requests or budget.max_chat_input_bytes:
            raise ConfirmationEvidenceError("embedding budget contains chat limits")


def _validate_ablation_coordinator_policy(
    policy: AblationCoordinatorPolicy,
) -> None:
    values = (
        policy.sample_size,
        policy.max_attempts,
        policy.max_requests,
        policy.request_timeout_milliseconds,
        policy.total_timeout_milliseconds,
    )
    if any(value <= 0 for value in values):
        raise ConfirmationEvidenceError("ablation coordinator policy must be positive")
    minimum_requests = policy.sample_size * 3
    if (
        not minimum_requests
        <= policy.max_requests
        <= (minimum_requests * policy.max_attempts)
    ):
        raise ConfirmationEvidenceError(
            "ablation coordinator request cap is inconsistent"
        )
    if policy.total_timeout_milliseconds < policy.request_timeout_milliseconds:
        raise ConfirmationEvidenceError(
            "ablation coordinator total timeout is shorter than one request"
        )


def _binary_product(*values: int | None) -> int:
    if any(value not in {0, FACTOR_SCALE} for value in values):
        raise ConfirmationEvidenceError("semantic gates must be binary")
    return 0 if 0 in values else FACTOR_SCALE


def _round_ratio(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ConfirmationEvidenceError("cannot round a negative score ratio")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def _round_decimal(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _digest(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _go_ablation_profile_digest(fields: tuple[tuple[str, object], ...]) -> str:
    """Mirror Go ``encoding/json`` for the ordered frozen-profile struct.

    Thresholds are the only float64 fields. They originate as integer micros,
    so fixed-point rendering avoids Python/Go exponent drift at one micro.
    """
    rendered: list[str] = []
    for key, value in fields:
        encoded_key = json.dumps(key, ensure_ascii=False)
        if key in {"inference_threshold", "embedding_threshold"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfirmationEvidenceError(
                    "ablation threshold is not integer micros"
                )
            encoded_value = _go_micros_json(value)
        else:
            encoded_value = canonical_json(value).decode()
        rendered.append(f"{encoded_key}:{encoded_value}")
    return hashlib.sha256(("{" + ",".join(rendered) + "}").encode()).hexdigest()


def _go_micros_json(value: int) -> str:
    if not 0 <= value <= SCORE_SCALE:
        raise ConfirmationEvidenceError("ablation threshold is out of range")
    if value == 0:
        return "0"
    if value == SCORE_SCALE:
        return "1"
    return f"0.{value:06d}".rstrip("0")


def _require_text(value: str, label: str) -> None:
    if not value or value != value.strip() or len(value) > 256:
        raise ConfirmationEvidenceError(f"{label} is invalid")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or value.lower() != value:
        raise ConfirmationEvidenceError(f"{label} is not lowercase SHA-256")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise ConfirmationEvidenceError(f"{label} is not lowercase SHA-256") from error


__all__ = [
    "AblationCoordinatorPolicy",
    "AblationVerificationPolicy",
    "ABLATION_PROFILE_CONTRACT_VERSION",
    "CAPABILITY_ORDER",
    "CompositeVerificationPolicy",
    "ConfirmationEvidenceError",
    "ConfirmationVerificationProfile",
    "EmbeddingLanePolicy",
    "LONGMEM_SELECTOR_REVISION_V1",
    "ProviderLanePolicy",
    "SubjectProjection",
    "SyntheticBudgetPolicy",
    "VerifiedConfirmationEvidence",
    "canonical_json",
    "compute_subject_projection",
    "confirmation_inference_cap_requirements",
    "confirmation_signing_message",
    "evidence_digest",
    "rebuild_confirmation_evidence",
    "validate_confirmation_inference_caps",
]

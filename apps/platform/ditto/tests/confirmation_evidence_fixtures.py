"""Deterministic hostile-test fixtures for v9 confirmation evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import bittensor

from ditto.api_models.confirmation_bundles import (
    AblationDimensionEnvelope,
    AblationEvidence,
    AblationSyntheticUsage,
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationCompletionReport,
    LongMemCapabilityScore,
    LongMemDimensionEnvelope,
    LongMemEvidence,
    LongMemProviderLaneEvidence,
    LongMemScoreEvidence,
)
from ditto.api_server.confirmation_evidence import (
    CAPABILITY_ORDER,
    LONGMEM_SELECTOR_REVISION_V1,
    AblationCoordinatorPolicy,
    AblationVerificationPolicy,
    CompositeVerificationPolicy,
    ConfirmationVerificationProfile,
    EmbeddingLanePolicy,
    ProviderLanePolicy,
    SyntheticBudgetPolicy,
    confirmation_signing_message,
    evidence_digest,
    rebuild_confirmation_evidence,
)

if TYPE_CHECKING:
    from ditto.db.models import (
        ConfirmationBundle,
        ConfirmationBundleTicket,
    )

ARTIFACT_SHA256 = "a" * 64
BASE_EVIDENCE_SHA256 = "b" * 64
LONGMEM_DATASET_SHA256 = "d" * 64
THRESHOLD_MANIFEST_SHA256 = "1" * 64
COORDINATOR_SHA256 = "2" * 64
ABLATION_DATASET_SHA256 = "3" * 64
ABLATION_SELECTION_KEY_SHA256 = "9" * 64
ABLATION_PROJECTION_KEY_SHA256 = "0" * 64
CASE_SET_SHA256 = "4" * 64
BASELINE_SHA256 = "5" * 64
ABLATED_SHA256 = "6" * 64
RECEIPT_READER_SHA256 = "7" * 64
RECEIPT_JUDGE_SHA256 = "8" * 64

VALIDATOR_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")


class BaseProofKwargs(TypedDict):
    base_evidence_sha256: str
    base_quality_micros: int
    base_stderr_micros: int
    base_model_factor_bps: int
    base_tool_factor_bps: int


def verification_profile() -> ConfirmationVerificationProfile:
    inference_budget = SyntheticBudgetPolicy(
        max_chat_requests=8,
        max_chat_input_bytes=4096,
        max_embedding_requests=0,
        max_embedding_inputs=0,
        max_embedding_input_bytes=0,
    )
    embedding_budget = SyntheticBudgetPolicy(
        max_chat_requests=0,
        max_chat_input_bytes=0,
        max_embedding_requests=8,
        max_embedding_inputs=16,
        max_embedding_input_bytes=4096,
    )
    profile = ConfirmationVerificationProfile(
        schema_version=1,
        revision="confirmation-v9-test-1",
        longmem_profile_revision="longmem-v9-test-1",
        longmem_profile_checksum="0" * 64,
        longmem_dataset_revision="longmemeval-s-test-1",
        longmem_dataset_sha256=LONGMEM_DATASET_SHA256,
        longmem_selector_revision=LONGMEM_SELECTOR_REVISION_V1,
        longmem_selection_seed=387,
        longmem_cases_per_capability=2,
        longmem_seed_batch_pairs=64,
        longmem_projection_key_sha256="e" * 64,
        provider_lanes=(
            ProviderLanePolicy(
                lane="reader",
                provider="openai",
                route_provider="openai",
                receipt_provider="OpenAI",
                profile_revision="reader-test-1",
                model="openai/gpt-oss-20b",
                max_requests=10,
                max_prompt_tokens=1_000,
                max_completion_tokens=1_000,
                max_total_tokens=2_000,
                max_cost_usd_micros=100_000,
            ),
            ProviderLanePolicy(
                lane="judge",
                provider="openai",
                route_provider="openai",
                receipt_provider="OpenAI",
                profile_revision="judge-test-1",
                model="openai/gpt-oss-20b",
                max_requests=10,
                max_prompt_tokens=1_000,
                max_completion_tokens=1_000,
                max_total_tokens=2_000,
                max_cost_usd_micros=100_000,
            ),
        ),
        embedding_lane=EmbeddingLanePolicy(
            lane="embedding",
            provider="perplexity",
            profile_revision="embedding-test-1",
            model="perplexity/pplx-embed-v1-0.6b",
            dimensions=768,
            max_requests=1_000,
            max_input_tokens=1_000_000,
            max_cost_usd_micros=100_000,
        ),
        ablation_profile_revision="ablation-v9-test-1",
        ablation_profile_checksum="0" * 64,
        ablation_dataset_sha256=ABLATION_DATASET_SHA256,
        ablation_threshold_manifest_sha256=THRESHOLD_MANIFEST_SHA256,
        ablation_selection_key_sha256=ABLATION_SELECTION_KEY_SHA256,
        ablation_projection_key_sha256=ABLATION_PROJECTION_KEY_SHA256,
        ablation_coordinator_policy=AblationCoordinatorPolicy(
            sample_size=2,
            max_attempts=1,
            max_requests=6,
            request_timeout_milliseconds=1_000,
            total_timeout_milliseconds=5_000,
        ),
        inference_ablation=AblationVerificationPolicy(
            intervention="inference",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=inference_budget,
        ),
        embedding_ablation=AblationVerificationPolicy(
            intervention="embedding",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=embedding_budget,
        ),
        composite=CompositeVerificationPolicy(
            schema_version=1,
            revision="composite-test-1",
            formula_revision="weighted-quality-gates-v1",
            base_weight_bps=6_000,
            longmem_weight_bps=4_000,
        ),
    )
    return replace(
        profile,
        longmem_profile_checksum=profile.longmem_checksum(),
        ablation_profile_checksum=profile.ablation_checksum(),
    )


def go_verification_profile() -> ConfirmationVerificationProfile:
    """Exact non-secret profile that produced the committed Go wire fixture."""
    profile = ConfirmationVerificationProfile(
        schema_version=1,
        revision="confirmation-launch-v1",
        longmem_profile_revision="longmem-launch-v1",
        longmem_profile_checksum="0" * 64,
        longmem_dataset_revision="longmemeval-s-2026-08-08",
        longmem_dataset_sha256="b" * 64,
        longmem_selector_revision=LONGMEM_SELECTOR_REVISION_V1,
        longmem_selection_seed=387,
        longmem_cases_per_capability=2,
        longmem_seed_batch_pairs=64,
        longmem_projection_key_sha256=(
            "b8ac4b9f08133323931736991692cb99f3cc4806d3baafc367f6646130f3552d"
        ),
        provider_lanes=(
            *(
                ProviderLanePolicy(
                    lane=lane,
                    provider="openrouter",
                    route_provider="openai",
                    receipt_provider="OpenAI",
                    profile_revision=f"longmem-{lane}-v1",
                    model="openai/gpt-oss-20b",
                    max_requests=20,
                    max_prompt_tokens=2_000,
                    max_completion_tokens=400,
                    max_total_tokens=2_400,
                    max_cost_usd_micros=50_000,
                )
                for lane in ("judge", "reader")
            ),
        ),
        embedding_lane=EmbeddingLanePolicy(
            lane="embedding",
            provider="perplexity",
            profile_revision="embedding-launch-v1",
            model="perplexity/pplx-embed-v1-0.6b",
            dimensions=768,
            max_requests=1_000,
            max_input_tokens=1_000_000,
            max_cost_usd_micros=100_000,
        ),
        ablation_profile_revision="ablation-launch-v1",
        ablation_profile_checksum="0" * 64,
        ablation_dataset_sha256="c" * 64,
        ablation_threshold_manifest_sha256="d" * 64,
        ablation_selection_key_sha256=(
            "a40c0307501738f478a81ee68799f195a140cc93eb4f9b644f222c237c202cef"
        ),
        ablation_projection_key_sha256=(
            "b8ac4b9f08133323931736991692cb99f3cc4806d3baafc367f6646130f3552d"
        ),
        ablation_coordinator_policy=AblationCoordinatorPolicy(
            sample_size=2,
            max_attempts=1,
            max_requests=6,
            request_timeout_milliseconds=1_000,
            total_timeout_milliseconds=5_000,
        ),
        inference_ablation=AblationVerificationPolicy(
            intervention="inference",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=4,
                max_chat_input_bytes=256,
                max_embedding_requests=0,
                max_embedding_inputs=0,
                max_embedding_input_bytes=0,
            ),
        ),
        embedding_ablation=AblationVerificationPolicy(
            intervention="embedding",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=0,
                max_chat_input_bytes=0,
                max_embedding_requests=4,
                max_embedding_inputs=4,
                max_embedding_input_bytes=256,
            ),
        ),
        composite=CompositeVerificationPolicy(
            schema_version=1,
            revision="composite-launch-v1",
            formula_revision="weighted-quality-gates-v1",
            base_weight_bps=5_000,
            longmem_weight_bps=5_000,
        ),
    )
    assert profile.longmem_checksum() == (
        "413d8c26a17834ff60f3243b5f96b24dd9a18deaf7d0ee21cb3c642c3e024927"
    )
    assert profile.ablation_checksum() == (
        "1801748138fb6d3e1b37f54a8a9f1db994c92564ecb62818b9c9c04290a77dbe"
    )
    return replace(
        profile,
        longmem_profile_checksum=profile.longmem_checksum(),
        ablation_profile_checksum=profile.ablation_checksum(),
    )


def go_installed_verification_profile() -> ConfirmationVerificationProfile:
    """Exact profile used by Go's installed-runtime contract vector."""
    profile = ConfirmationVerificationProfile(
        schema_version=1,
        revision="v9-launch-calibrated-2026-08-08",
        longmem_profile_revision="longmemeval-launch-v1",
        longmem_profile_checksum="0" * 64,
        longmem_dataset_revision=(
            "huggingface-98d7416c24c778c2fee6e6f3006e7a073259d48f-"
            "longmemeval-9e0b455f4ef0e2ab8f2e582289761153549043fc"
        ),
        longmem_dataset_sha256=(
            "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
        ),
        longmem_selector_revision=LONGMEM_SELECTOR_REVISION_V1,
        longmem_selection_seed=17,
        longmem_cases_per_capability=2,
        longmem_seed_batch_pairs=2,
        longmem_projection_key_sha256=(
            "4113d54b0b611294b7f595b691c9db541fc0fc719848d6c5c34522eacc0b3a24"
        ),
        provider_lanes=tuple(
            ProviderLanePolicy(
                lane=lane,
                provider="pinned-provider",
                route_provider="openai",
                receipt_provider="OpenAI",
                profile_revision="provider-launch-v1",
                model="pinned-model",
                max_requests=10,
                max_prompt_tokens=100,
                max_completion_tokens=100,
                max_total_tokens=200,
                max_cost_usd_micros=10_000,
            )
            for lane in ("judge", "reader")
        ),
        embedding_lane=EmbeddingLanePolicy(
            lane="embedding",
            provider="pinned-provider",
            profile_revision="embedding-launch-v1",
            model="pinned-embedding-model",
            dimensions=768,
            max_requests=1_000,
            max_input_tokens=1_000_000,
            max_cost_usd_micros=100_000,
        ),
        ablation_profile_revision="ablation-launch-v1",
        ablation_profile_checksum="0" * 64,
        ablation_dataset_sha256="a" * 64,
        ablation_threshold_manifest_sha256="b" * 64,
        ablation_selection_key_sha256=(
            "22a48051594c1949deed7040850c1f0f8764537f5191be56732d16a54c1d8153"
        ),
        ablation_projection_key_sha256=(
            "425ed4e4a36b30ea21b90e21c712c649e8214c29b7eaf68089d1039c6e55384c"
        ),
        ablation_coordinator_policy=AblationCoordinatorPolicy(
            sample_size=2,
            max_attempts=1,
            max_requests=6,
            request_timeout_milliseconds=50,
            total_timeout_milliseconds=100,
        ),
        inference_ablation=AblationVerificationPolicy(
            intervention="inference",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=10,
                max_chat_input_bytes=10_000,
                max_embedding_requests=0,
                max_embedding_inputs=0,
                max_embedding_input_bytes=0,
            ),
        ),
        embedding_ablation=AblationVerificationPolicy(
            intervention="embedding",
            contract_version="dittobench-v9-ablation-v1",
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=0,
                max_chat_input_bytes=0,
                max_embedding_requests=10,
                max_embedding_inputs=10,
                max_embedding_input_bytes=10_000,
            ),
        ),
        composite=CompositeVerificationPolicy(
            schema_version=1,
            revision="composite-launch-v1",
            formula_revision="weighted-quality-gates-v1",
            base_weight_bps=7_000,
            longmem_weight_bps=3_000,
        ),
    )
    return replace(
        profile,
        longmem_profile_checksum=profile.longmem_checksum(),
        ablation_profile_checksum=profile.ablation_checksum(),
    )


def active_settings(
    *, mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW
) -> ConfirmationBundleSettings:
    profile = verification_profile()
    return ConfirmationBundleSettings(
        mode=mode,
        top_n=5,
        daily_bundle_cap=10,
        daily_dollar_cap_microusd=1_000_000,
        per_bundle_request_cap=100,
        per_bundle_token_cap=10_000,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        challenger_z=1.64,
    )


def base_proof_kwargs(
    *, quality_micros: int = 800_000, stderr_micros: int = 20_000
) -> BaseProofKwargs:
    return {
        "base_evidence_sha256": BASE_EVIDENCE_SHA256,
        "base_quality_micros": quality_micros,
        "base_stderr_micros": stderr_micros,
        "base_model_factor_bps": 10_000,
        "base_tool_factor_bps": 10_000,
    }


def longmem_envelope(
    *, artifact_sha256: str = ARTIFACT_SHA256
) -> LongMemDimensionEnvelope:
    scores = [
        LongMemCapabilityScore(
            capability=capability,
            correct=1,
            count=2,
            mean_micros=500_000,
        )
        for capability in CAPABILITY_ORDER
    ]
    lanes = [
        LongMemProviderLaneEvidence(
            lane="judge",
            cost_source="provider_receipt_v1",
            currency="USD",
            provider="openai",
            profile_revision="judge-test-1",
            model="openai/gpt-oss-20b",
            fallback_used=False,
            requests=1,
            successes=1,
            receipted_requests=1,
            prompt_tokens=50,
            completion_tokens=10,
            total_tokens=60,
            cost_usd_micros=5_000,
            receipt_set_sha256=RECEIPT_JUDGE_SHA256,
        ),
        LongMemProviderLaneEvidence(
            lane="reader",
            cost_source="provider_receipt_v1",
            currency="USD",
            provider="openai",
            profile_revision="reader-test-1",
            model="openai/gpt-oss-20b",
            fallback_used=False,
            requests=2,
            successes=2,
            receipted_requests=2,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cost_usd_micros=10_000,
            receipt_set_sha256=RECEIPT_READER_SHA256,
        ),
    ]
    evidence = LongMemEvidence(
        schema_version=2,
        artifact_sha256=artifact_sha256,
        bench_version=9,
        profile_checksum=verification_profile().longmem_profile_checksum,
        case_set_digest=CASE_SET_SHA256,
        dataset_revision="longmemeval-s-test-1",
        dataset_sha256=LONGMEM_DATASET_SHA256,
        score=LongMemScoreEvidence(
            longmem_mean_micros=500_000,
            longmem_stderr_micros=204_124,
            case_count=12,
            per_capability=scores,
        ),
        provider_evidence=lanes,
    )
    return LongMemDimensionEnvelope(
        status="completed",
        evidence_sha256=evidence_digest(evidence),
        latency_ms=1234,
        request_count=3,
        input_tokens=150,
        output_tokens=30,
        provider_cost_microusd=15_000,
        synthetic=False,
        evidence=evidence,
    )


def ablation_envelope(
    intervention: Literal["inference", "embedding"],
    *,
    mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
    status: Literal["passed", "failed", "unavailable", "not_run"] = "passed",
    artifact_sha256: str = ARTIFACT_SHA256,
) -> AblationDimensionEnvelope:
    profile = verification_profile()
    policy = (
        profile.inference_ablation
        if intervention == "inference"
        else profile.embedding_ablation
    )
    baseline: int | None
    ablated: int | None
    delta: int | None
    if status == "passed":
        baseline, ablated, delta = 800_000, 500_000, 300_000
    elif status == "failed":
        baseline, ablated, delta = 700_000, 600_000, 100_000
    else:
        baseline = ablated = delta = None
    semantic = None if baseline is None else int(status == "passed") * 10_000
    applied = (
        None
        if semantic is None
        else 10_000
        if mode == ConfirmationBundleMode.SHADOW
        else semantic
    )
    inference = intervention == "inference"
    affected = 0 if baseline is None else 1
    budget_exhausted = status == "unavailable"
    usage = AblationSyntheticUsage(
        synthetic=True,
        intervention=intervention,
        budget=policy.budget.wire(),
        chat_attempts=(affected + int(budget_exhausted)) if inference else 0,
        chat_applied=affected if inference else 0,
        chat_input_bytes=64 if inference and affected else 0,
        embedding_attempts=(affected + int(budget_exhausted)) if not inference else 0,
        embedding_applied=affected if not inference else 0,
        embedding_inputs=affected if not inference else 0,
        embedding_input_bytes=64 if not inference and affected else 0,
        rejected_requests=int(budget_exhausted),
        budget_exhausted=budget_exhausted,
        upstream_requests=0,
        upstream_input_tokens=0,
        upstream_output_tokens=0,
        upstream_provider_cost_microusd=0,
    )
    evidence = AblationEvidence(
        contract_version=policy.contract_version,
        bench_version=9,
        artifact_sha256=artifact_sha256,
        intervention=intervention,
        mode=mode.value,
        status=status,
        reason={
            "passed": "threshold_met",
            "failed": "delta_below_threshold",
            "unavailable": "budget_exhausted",
            "not_run": "disabled",
        }[status],
        profile_revision=profile.ablation_profile_revision,
        profile_checksum=profile.ablation_profile_checksum,
        threshold_manifest_sha256=profile.ablation_threshold_manifest_sha256,
        coordinator_sha256=COORDINATOR_SHA256,
        dataset_sha256=profile.ablation_dataset_sha256,
        case_set_sha256=CASE_SET_SHA256,
        baseline_scores_sha256=BASELINE_SHA256 if baseline is not None else None,
        ablated_scores_sha256=ABLATED_SHA256 if baseline is not None else None,
        baseline_mean_micros=baseline,
        ablated_mean_micros=ablated,
        delta_micros=delta,
        threshold_micros=policy.threshold_micros,
        sample_count=2 if baseline is not None else 0,
        affected_call_count=affected,
        semantic_factor_bps=semantic,
        applied_factor_bps=applied,
        synthetic_usage=usage,
    )
    envelope_status: Literal["completed", "unavailable", "not_run"]
    envelope_status = (
        "completed"
        if status in {"passed", "failed"}
        else cast(Literal["unavailable", "not_run"], status)
    )
    return AblationDimensionEnvelope(
        status=envelope_status,
        evidence_sha256=evidence_digest(evidence),
        latency_ms=200,
        request_count=0,
        input_tokens=0,
        output_tokens=0,
        provider_cost_microusd=0,
        synthetic=True,
        evidence=evidence,
    )


def unsigned_report(
    *,
    artifact_sha256: str = ARTIFACT_SHA256,
    mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
    inference_status: Literal["passed", "failed", "unavailable", "not_run"] = "passed",
    embedding_status: Literal["passed", "failed", "unavailable", "not_run"] = "passed",
) -> ConfirmationCompletionReport:
    return ConfirmationCompletionReport(
        ablation_coordinator_latency_ms=200,
        longmemeval=longmem_envelope(artifact_sha256=artifact_sha256),
        inference_ablation=ablation_envelope(
            "inference",
            mode=mode,
            status=inference_status,
            artifact_sha256=artifact_sha256,
        ),
        embedding_ablation=ablation_envelope(
            "embedding",
            mode=mode,
            status=embedding_status,
            artifact_sha256=artifact_sha256,
        ),
        bundle_signature="00",
    )


def signed_report(
    *,
    bundle: ConfirmationBundle,
    ticket: ConfirmationBundleTicket,
    mode: ConfirmationBundleMode,
    inference_status: Literal["passed", "failed", "unavailable", "not_run"] = "passed",
    embedding_status: Literal["passed", "failed", "unavailable", "not_run"] = "passed",
    keypair: bittensor.Keypair = VALIDATOR_KEYPAIR,
) -> ConfirmationCompletionReport:
    profile = verification_profile()
    report = unsigned_report(
        artifact_sha256=bundle.artifact_sha256,
        mode=mode,
        inference_status=inference_status,
        embedding_status=embedding_status,
    )
    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=bundle.artifact_sha256,
        profile_revision=bundle.profile_revision,
        profile_checksum=bundle.profile_checksum,
        settings_revision=bundle.settings_revision,
        settings_checksum=bundle.settings_checksum,
        retest_generation=bundle.retest_generation,
        mode=mode,
        profile=profile,
    )
    message = confirmation_signing_message(
        reporter_hotkey=ticket.validator_hotkey,
        bundle_id=bundle.bundle_id,
        ticket_id=ticket.ticket_id,
        deadline=ticket.deadline,
        artifact_sha256=bundle.artifact_sha256,
        profile_revision=bundle.profile_revision,
        profile_checksum=bundle.profile_checksum,
        settings_revision=bundle.settings_revision,
        settings_checksum=bundle.settings_checksum,
        retest_generation=bundle.retest_generation,
        evidence_sha256=verified.evidence_sha256,
    )
    return report.model_copy(update={"bundle_signature": keypair.sign(message).hex()})


__all__ = [
    "ARTIFACT_SHA256",
    "BASE_EVIDENCE_SHA256",
    "VALIDATOR_KEYPAIR",
    "ablation_envelope",
    "active_settings",
    "base_proof_kwargs",
    "go_verification_profile",
    "go_installed_verification_profile",
    "longmem_envelope",
    "signed_report",
    "unsigned_report",
    "verification_profile",
]

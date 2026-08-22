"""Cross-language contract vectors for Go Bench v9 confirmation evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ditto.api_models.confirmation_bundles import ConfirmationBundleMode
from ditto.api_models.validator_confirmation import ConfirmationExecutionProfile
from ditto.api_server.confirmation_evidence import (
    ABLATION_EVIDENCE_CONTRACT_VERSION,
    ABLATION_PROFILE_CONTRACT_VERSION,
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    canonical_json,
    evidence_digest,
    rebuild_confirmation_evidence,
)
from ditto.api_server.confirmation_wire import (
    ConfirmationWireError,
    ablation_envelope_from_go,
    completion_report_from_go_dimensions,
    completion_report_from_go_fixture,
)
from ditto.tests.confirmation_evidence_fixtures import (
    go_installed_verification_profile,
    go_verification_profile,
)

FIXTURE_PATH = (
    Path(__file__).parents[5]
    / "services"
    / "dittobench-api"
    / "internal"
    / "confirmationwire"
    / "testdata"
    / "go_confirmation_evidence_v9.json"
)
UNAVAILABLE_ABLATION_PATH = FIXTURE_PATH.with_name("go_ablation_unavailable_v9.json")
OBSERVATIONAL_DROP_PATH = FIXTURE_PATH.with_name(
    "go_ablation_observational_drop_v9.json"
)
EXECUTION_PROFILE_FIXTURE_PATH = (
    Path(__file__).parents[5]
    / "services"
    / "dittobench-api"
    / "cmd"
    / "dittobench-api"
    / "testdata"
    / "confirmation_execution_profile_v9.json"
)
ARTIFACT_SHA256 = "a" * 64
SETTINGS_SHA256 = "f" * 64
ABLATION_COORDINATOR_LATENCY_MS = 444


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text())


def _verification_profile() -> ConfirmationVerificationProfile:
    report = completion_report_from_go_fixture(
        _fixture(),
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    inference = report.inference_ablation.evidence
    embedding = report.embedding_ablation.evidence
    profile = go_verification_profile()
    assert profile.longmem_checksum() == report.longmemeval.evidence.profile_checksum
    assert profile.ablation_checksum() == inference.profile_checksum
    assert inference.profile_checksum == embedding.profile_checksum
    return profile


def test_outer_execution_profile_checksum_matches_shared_go_vector() -> None:
    fixture = json.loads(EXECUTION_PROFILE_FIXTURE_PATH.read_text())
    assert fixture["fixture_schema"] == (
        "dittobench-v9-confirmation-execution-profile-v1"
    )
    profile = go_installed_verification_profile()
    expected_checksum = fixture["expected_checksum"]
    fixture_profile = fixture["profile"]
    wire = ConfirmationExecutionProfile.model_validate(
        {**fixture_profile, "checksum": expected_checksum}
    )

    assert fixture_profile == profile.payload()
    assert canonical_json(fixture_profile) == canonical_json(profile.payload())
    assert evidence_digest(fixture_profile) == expected_checksum
    assert profile.checksum() == expected_checksum
    assert wire.checksum == expected_checksum
    assert wire.model_dump(mode="json", exclude={"checksum"}) == fixture_profile


def test_real_go_evidence_converts_and_rebuilds_confirmation_root() -> None:
    report = completion_report_from_go_fixture(
        _fixture(),
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    profile = _verification_profile()

    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        settings_revision=7,
        settings_checksum=SETTINGS_SHA256,
        retest_generation=3,
        mode=ConfirmationBundleMode.SHADOW,
        profile=profile,
    )

    assert report.longmemeval.evidence.schema_version == 2
    assert report.longmemeval.evidence.score.longmem_mean_micros == 500_000
    assert report.longmemeval.evidence.score.longmem_stderr_micros == 204_124
    assert report.inference_ablation.evidence.delta_micros == 100_000
    assert report.embedding_ablation.evidence.delta_micros == 100_000
    assert verified.longmem_mean_micros == 500_000
    assert verified.longmem_stderr_micros == 204_124
    assert verified.ablations_complete is True
    assert verified.ablation_semantic_factor_bps == 0
    assert verified.root.totals.model_dump() == {
        "request_count": 24,
        "input_tokens": 2_200,
        "output_tokens": 220,
        "provider_cost_microusd": 22_345,
        "latency_ms": 4_765,
    }
    assert report.inference_ablation.latency_ms == 111
    assert report.embedding_ablation.latency_ms == 222
    assert verified.root.ablation_coordinator_latency_ms == 444
    assert verified.evidence_sha256 == (
        "1d99e2aafd3b1effd4179d966674087883496065514bdfb14b15551f8a68a824"
    )


def test_live_v4_profile_contract_accepts_go_evidence_contract() -> None:
    """Live v4 canaries 409ed here: policy is ablation-profile-v1, evidence is v1.

    Attempts 3 and 4 on bundle a98db16f persisted ``ablation_profile_drift``
    after execute. The frozen execution profile names the *profile* contract;
    Go evidence names the *evidence* contract. Those strings are not equal.
    """
    report = completion_report_from_go_fixture(
        _fixture(),
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    assert report.inference_ablation.evidence.contract_version == (
        ABLATION_EVIDENCE_CONTRACT_VERSION
    )
    profile = _verification_profile()
    live = replace(
        profile,
        inference_ablation=replace(
            profile.inference_ablation,
            contract_version=ABLATION_PROFILE_CONTRACT_VERSION,
        ),
        embedding_ablation=replace(
            profile.embedding_ablation,
            contract_version=ABLATION_PROFILE_CONTRACT_VERSION,
        ),
    )

    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=live.revision,
        profile_checksum=live.checksum(),
        settings_revision=7,
        settings_checksum=SETTINGS_SHA256,
        retest_generation=3,
        mode=ConfirmationBundleMode.SHADOW,
        profile=live,
    )
    assert verified.ablations_complete is True


def test_unknown_ablation_evidence_contract_still_fails_closed() -> None:
    report = completion_report_from_go_fixture(
        _fixture(),
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    inference = report.inference_ablation.evidence.model_copy(
        update={"contract_version": "dittobench-v9-ablation-profile-v1"}
    )
    broken = report.model_copy(
        update={
            "inference_ablation": report.inference_ablation.model_copy(
                update={
                    "evidence": inference,
                    "evidence_sha256": evidence_digest(inference),
                }
            )
        }
    )
    with pytest.raises(ConfirmationEvidenceError, match="ablation profile drift"):
        rebuild_confirmation_evidence(
            broken,
            artifact_sha256=ARTIFACT_SHA256,
            profile_revision=_verification_profile().revision,
            profile_checksum=_verification_profile().checksum(),
            settings_revision=7,
            settings_checksum=SETTINGS_SHA256,
            retest_generation=3,
            mode=ConfirmationBundleMode.SHADOW,
            profile=_verification_profile(),
        )


def test_adapter_drops_only_verified_private_go_fields() -> None:
    report = completion_report_from_go_fixture(
        _fixture(),
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    inference = report.inference_ablation.evidence.model_dump()
    usage = inference["synthetic_usage"]

    assert "selected_cases_sha256" not in inference
    assert "ablation_profile_sha256" not in inference
    assert "telemetry_namespace" not in usage
    assert inference["profile_checksum"] == (
        "1801748138fb6d3e1b37f54a8a9f1db994c92564ecb62818b9c9c04290a77dbe"
    )
    assert usage["upstream_requests"] == 0
    assert usage["upstream_provider_cost_microusd"] == 0


@pytest.mark.parametrize(
    "mode", [ConfirmationBundleMode.SHADOW, ConfirmationBundleMode.ENFORCE]
)
def test_real_go_shape_unavailable_evidence_is_preserved_fail_closed(
    mode: ConfirmationBundleMode,
) -> None:
    fixture = _fixture()
    for dimension_name in ("inference_ablation", "embedding_ablation"):
        dimension = fixture[dimension_name]
        assert isinstance(dimension, dict)
        evidence = dimension["evidence"]
        assert isinstance(evidence, dict)
        evidence["mode"] = mode.value
        evidence["status"] = "unavailable"
        evidence["reason"] = "counterfactual_proof_unavailable"
        evidence.pop("baseline_scores_sha256")
        evidence.pop("ablated_scores_sha256")
        for key in (
            "baseline_mean",
            "ablated_mean",
            "delta",
            "semantic_factor",
            "applied_factor",
        ):
            evidence[key] = 0
        evidence["sample_count"] = 0
        evidence["affected_call_count"] = 0
        usage = evidence["synthetic_usage"]
        assert isinstance(usage, dict)
        lane_fields = (
            ("chat_attempts", "chat_applied", "chat_input_bytes")
            if dimension_name == "inference_ablation"
            else (
                "embedding_attempts",
                "embedding_applied",
                "embedding_inputs",
                "embedding_input_bytes",
            )
        )
        for key in lane_fields:
            # These explicit zeros are the exact Go wire contract. Omitting
            # them must fail before Platform report preparation.
            usage[key] = 0
        missing = copy.deepcopy(evidence)
        missing_usage = missing["synthetic_usage"]
        assert isinstance(missing_usage, dict)
        missing_usage.pop(lane_fields[0])
        with pytest.raises(ConfirmationWireError, match="fields drifted"):
            broken = copy.deepcopy(fixture)
            broken_dimension = broken[dimension_name]
            assert isinstance(broken_dimension, dict)
            broken_dimension["evidence"] = missing
            completion_report_from_go_fixture(
                broken,
                ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
            )
        canonical = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":")
        ).encode()
        dimension["go_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()

    report = completion_report_from_go_fixture(
        fixture,
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
    )
    assert report.inference_ablation.status == "unavailable"
    assert report.embedding_ablation.status == "unavailable"
    assert report.inference_ablation.evidence.semantic_factor_bps is None
    assert report.embedding_ablation.evidence.applied_factor_bps is None

    profile = _verification_profile()
    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        settings_revision=7,
        settings_checksum=SETTINGS_SHA256,
        retest_generation=3,
        mode=mode,
        profile=profile,
    )
    assert verified.ablations_complete is False
    assert verified.ablation_semantic_factor_bps is None


def test_production_unavailable_ablation_rebuilds_with_fixture_longmem() -> None:
    fixture = _fixture()
    unavailable = json.loads(UNAVAILABLE_ABLATION_PATH.read_text())
    longmemeval = fixture["longmemeval"]
    inference = unavailable["inference"]
    embedding = unavailable["embedding"]
    assert isinstance(longmemeval, dict)
    assert isinstance(inference, dict)
    assert isinstance(embedding, dict)
    report = completion_report_from_go_dimensions(
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
        longmemeval=longmemeval,
        inference_ablation=inference,
        embedding_ablation=embedding,
    )
    assert report.inference_ablation.status == "unavailable"
    assert report.embedding_ablation.status == "unavailable"
    assert report.inference_ablation.evidence.sample_count == 2
    assert report.embedding_ablation.evidence.affected_call_count == 4
    assert (
        ablation_envelope_from_go(
            inference, expected_intervention="inference"
        ).evidence_sha256
        == report.inference_ablation.evidence_sha256
    )

    profile = _verification_profile()
    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        settings_revision=7,
        settings_checksum=SETTINGS_SHA256,
        retest_generation=3,
        mode=ConfirmationBundleMode.SHADOW,
        profile=profile,
    )
    assert verified.ablations_complete is False
    assert verified.ablation_semantic_factor_bps is None
    assert verified.longmem_mean_micros == 500_000


def test_production_shadow_high_drop_qualifies_and_keeps_longmem_score() -> None:
    fixture = _fixture()
    drop = json.loads(OBSERVATIONAL_DROP_PATH.read_text())
    longmemeval = fixture["longmemeval"]
    inference = drop["inference"]
    embedding = drop["embedding"]
    assert isinstance(longmemeval, dict)
    assert isinstance(inference, dict)
    assert isinstance(embedding, dict)
    report = completion_report_from_go_dimensions(
        ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
        longmemeval=longmemeval,
        inference_ablation=inference,
        embedding_ablation=embedding,
    )
    assert report.inference_ablation.status == "completed"
    assert report.embedding_ablation.status == "completed"
    assert report.inference_ablation.evidence.reason == "observational_drop_not_causal"
    assert report.embedding_ablation.evidence.reason == "observational_drop_not_causal"

    profile = _verification_profile()
    verified = rebuild_confirmation_evidence(
        report,
        artifact_sha256=ARTIFACT_SHA256,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        settings_revision=7,
        settings_checksum=SETTINGS_SHA256,
        retest_generation=3,
        mode=ConfirmationBundleMode.SHADOW,
        profile=profile,
    )
    assert verified.ablations_complete is True
    assert verified.ablation_semantic_factor_bps == 0
    assert verified.longmem_mean_micros == 500_000


def test_self_consistent_v1_passed_wire_evidence_is_rejected() -> None:
    fixture = _fixture()
    dimension = fixture["inference_ablation"]
    assert isinstance(dimension, dict)
    evidence = dimension["evidence"]
    assert isinstance(evidence, dict)
    evidence.update(
        {
            "status": "passed",
            "reason": "threshold_met",
            "ablated_mean": 0.7,
            "delta": 0.2,
            "semantic_factor": 1,
        }
    )
    dimension["go_evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()

    with pytest.raises(ConfirmationWireError, match="unsupported Go ablation"):
        completion_report_from_go_fixture(
            fixture,
            ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
        )


def _legacy_schema_one(payload: dict[str, object]) -> None:
    longmem = payload["longmemeval"]
    assert isinstance(longmem, dict)
    evidence = longmem["evidence"]
    assert isinstance(evidence, dict)
    evidence["schema_version"] = 1


def _hand_authored_micros(payload: dict[str, object]) -> None:
    longmem = payload["longmemeval"]
    assert isinstance(longmem, dict)
    evidence = longmem["evidence"]
    assert isinstance(evidence, dict)
    score = evidence["score"]
    assert isinstance(score, dict)
    score["longmem_mean_micros"] = 500_000
    del score["longmem_mean"]


def _omit_private_coordinator_binding(payload: dict[str, object]) -> None:
    inference = payload["inference_ablation"]
    assert isinstance(inference, dict)
    evidence = inference["evidence"]
    assert isinstance(evidence, dict)
    del evidence["selected_cases_sha256"]


def _add_unrecognized_accounting(payload: dict[str, object]) -> None:
    longmem = payload["longmemeval"]
    assert isinstance(longmem, dict)
    evidence = longmem["evidence"]
    assert isinstance(evidence, dict)
    providers = evidence["provider_evidence"]
    assert isinstance(providers, list)
    provider = providers[0]
    assert isinstance(provider, dict)
    provider["estimated_cost_usd"] = 0.012345


@pytest.mark.parametrize(
    "mutate",
    [
        _legacy_schema_one,
        _hand_authored_micros,
        _omit_private_coordinator_binding,
        _add_unrecognized_accounting,
    ],
)
def test_historical_or_hand_authored_wire_drift_cannot_pass(mutate) -> None:
    payload = copy.deepcopy(_fixture())
    mutate(payload)

    with pytest.raises(ConfirmationWireError):
        completion_report_from_go_fixture(
            payload,
            ablation_coordinator_latency_ms=ABLATION_COORDINATOR_LATENCY_MS,
        )

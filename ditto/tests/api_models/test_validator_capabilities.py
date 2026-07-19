"""Validation boundaries for the fixed heartbeat protocol-v7 contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import ValidatorHeartbeatRequest
from ditto.api_models.validator_capabilities import (
    ComponentProvenance,
    ScorerBenchmarkCapability,
    ValidatorCapabilities,
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)

_DIGEST = "sha256:" + "12" * 32
_REVISION = "ab" * 20
_V7_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v7.json"
_V9_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v9.json"


def _component(
    provenance: ComponentProvenance = "signed_descriptor",
) -> ValidatorComponentIdentity:
    return ValidatorComponentIdentity(
        image_digest=_DIGEST,
        source_revision=_REVISION,
        version="1.2.3",
        provenance=provenance,
    )


def _components(
    provenance: ComponentProvenance = "signed_descriptor",
) -> dict[str, ValidatorComponentIdentity]:
    return {
        name: _component(provenance)
        for name in (
            "ditto_subnet",
            "dittobench_api",
            "sandbox_docker",
            "model_relay",
            "pylon",
            "ollama",
        )
    }


def test_managed_stack_requires_exact_six_signed_image_identities() -> None:
    stack = ValidatorStackIdentity(
        mode="managed",
        compose_schema=1,
        release_descriptor_digest=_DIGEST,
        components=ValidatorStackComponents(**_components()),
    )
    assert stack.components.ollama.image_digest == _DIGEST
    with pytest.raises(ValidationError):
        ValidatorStackComponents(**(_components() | {"unexpected": _component()}))
    with pytest.raises(ValidationError):
        ValidatorStackIdentity(
            mode="managed",
            compose_schema=1,
            release_descriptor_digest=_DIGEST,
            components=ValidatorStackComponents(**_components("local_unverified")),
        )


def test_source_stack_cannot_claim_signed_descriptor_provenance() -> None:
    with pytest.raises(ValidationError):
        ValidatorStackIdentity(
            mode="source",
            compose_schema=1,
            release_descriptor_digest=None,
            components=ValidatorStackComponents(**_components()),
        )


def test_screened_image_capabilities_reject_unsafe_combinations() -> None:
    with pytest.raises(ValidationError):
        ValidatorCapabilities(
            screened_images=False,
            require_screened_image=True,
            source_build_fallback=False,
            full_stack_managed=False,
            stack_updater=False,
            sandbox_egress_restricted=True,
            executor_isolation="privileged_dind",
        )
    with pytest.raises(ValidationError):
        ValidatorCapabilities(
            screened_images=True,
            require_screened_image=False,
            source_build_fallback=True,
            full_stack_managed=False,
            stack_updater=True,
            sandbox_egress_restricted=True,
            executor_isolation="privileged_dind",
        )
    with pytest.raises(ValidationError):
        ValidatorCapabilities(
            screened_images=True,
            require_screened_image=False,
            source_build_fallback=False,
            full_stack_managed=False,
            stack_updater=False,
            sandbox_egress_restricted=True,
            executor_isolation="privileged_dind",
        )
    with pytest.raises(ValidationError):
        ValidatorCapabilities(
            screened_images=True,
            require_screened_image=True,
            source_build_fallback=True,
            full_stack_managed=False,
            stack_updater=False,
            sandbox_egress_restricted=True,
            executor_isolation="privileged_dind",
        )


def test_component_rejects_empty_or_unbounded_identity() -> None:
    with pytest.raises(ValidationError):
        ValidatorComponentIdentity(provenance="local_unverified")
    with pytest.raises(ValidationError):
        ValidatorComponentIdentity(version="x" * 65, provenance="local_unverified")


def test_scorer_benchmark_capability_fails_closed_without_verified_identity() -> None:
    verified = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 3),
        observed_at=1,
        software_version="1.2.3",
        source_revision=_REVISION,
    )
    assert verified.supported_bench_versions == (2, 3)
    verified_v4 = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 3, 4),
        observed_at=1,
        software_version="1.2.3",
        source_revision=_REVISION,
    )
    assert verified_v4.supported_bench_versions == (2, 3, 4)
    # Every post-v2 advertisement must be identity-bound. Pinning only v3 here
    # let (2, 4) — and any later bump — skip the fresh-verified requirement.
    for versions in ((2, 3), (2, 4), (2, 3, 4), (4,), (3,)):
        for status in ("legacy_v2", "unreachable", "identity_mismatch"):
            with pytest.raises(ValidationError):
                ScorerBenchmarkCapability(
                    status=status, supported_bench_versions=versions
                )


def test_heartbeat_protocol_v7_requires_both_typed_identity_sections() -> None:
    payload = json.loads(_V7_VECTOR.read_text())["request"]
    payload["signature"] = "ab" * 64
    assert ValidatorHeartbeatRequest.model_validate(payload).protocol_version == 7

    for missing in ("capabilities", "stack"):
        incomplete = payload.copy()
        incomplete.pop(missing)
        with pytest.raises(ValidationError):
            ValidatorHeartbeatRequest.model_validate(incomplete)

    legacy = payload | {"protocol_version": 6}
    with pytest.raises(ValidationError):
        ValidatorHeartbeatRequest.model_validate(legacy)

    v8_without_scorer = payload | {"protocol_version": 8}
    with pytest.raises(ValidationError):
        ValidatorHeartbeatRequest.model_validate(v8_without_scorer)

    v8 = json.loads(json.dumps(v8_without_scorer))
    v8["capabilities"]["scorer_benchmarks"] = {
        "status": "fresh_verified",
        "supported_bench_versions": (2, 3),
        "observed_at": 1,
        "software_version": "1.2.3",
        "source_revision": _REVISION,
    }
    assert ValidatorHeartbeatRequest.model_validate(v8).protocol_version == 8

    v7_with_scorer = v8 | {"protocol_version": 7}
    with pytest.raises(ValidationError):
        ValidatorHeartbeatRequest.model_validate(v7_with_scorer)


def test_heartbeat_protocol_v9_requires_per_component_stack_health() -> None:
    for fixture in json.loads(_V9_VECTOR.read_text()).values():
        payload = fixture["request"] | {"signature": "ab" * 64}
        request = ValidatorHeartbeatRequest.model_validate_json(json.dumps(payload))
        assert request.protocol_version == 9
        assert request.stack_health is not None

        without_health = {
            key: value for key, value in payload.items() if key != "stack_health"
        }
        with pytest.raises(ValidationError):
            ValidatorHeartbeatRequest.model_validate_json(json.dumps(without_health))

        downgraded = payload | {"protocol_version": 8}
        with pytest.raises(ValidationError):
            ValidatorHeartbeatRequest.model_validate_json(json.dumps(downgraded))

        v8 = without_health | {"protocol_version": 8}
        assert (
            ValidatorHeartbeatRequest.model_validate_json(
                json.dumps(v8)
            ).protocol_version
            == 8
        )


def test_scorer_capability_gating_is_independent_of_stack_health() -> None:
    """v3 stays bound to the verified scorer predicate, not to sidecar health.

    A heartbeat whose optional sidecar probes failed (unknown/unreachable
    components) still validates and still advertises exactly the benchmark
    versions its *verified scorer identity* supports — component health can
    neither grant nor revoke the v3 predicate.
    """
    vectors = json.loads(_V9_VECTOR.read_text())
    managed = vectors["managed"]["request"] | {"signature": "ab" * 64}
    broken_sidecars = json.loads(json.dumps(managed))
    for name in ("sandbox_docker", "model_relay", "pylon", "ollama"):
        broken_sidecars["stack_health"][name] = {
            "health": "unknown",
            "required": True,
        }
    request = ValidatorHeartbeatRequest.model_validate_json(json.dumps(broken_sidecars))
    assert request.capabilities is not None
    scorer = request.capabilities.scorer_benchmarks
    assert scorer is not None
    assert scorer.status == "fresh_verified"
    assert scorer.supported_bench_versions == (2, 3)

    # And the inverse: healthy sidecars cannot conjure a post-v2 version out of
    # an unverified scorer identity — for any post-v2 version, not just v3.
    source = vectors["source"]["request"] | {"signature": "ab" * 64}
    for advertised in ([2, 3], [2, 4], [2, 3, 4]):
        unverified = json.loads(json.dumps(source))
        scorer_claim = unverified["capabilities"]["scorer_benchmarks"]
        scorer_claim["supported_bench_versions"] = advertised
        with pytest.raises(ValidationError):
            ValidatorHeartbeatRequest.model_validate_json(json.dumps(unverified))

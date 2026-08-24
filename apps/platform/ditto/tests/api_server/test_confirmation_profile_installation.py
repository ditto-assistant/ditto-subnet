"""Release-asset tests for the production Bench v9 confirmation profile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto.api_server import create_api_server
from ditto.api_server.confirmation_evidence import (
    CAPABILITY_ORDER,
    ConfirmationEvidenceError,
    confirmation_inference_cap_requirements,
    validate_confirmation_inference_caps,
)
from ditto.api_server.confirmation_profile_installation import (
    ConfirmationProfileInstallationError,
    decode_confirmation_verification_profile,
    installed_confirmation_verification_profiles,
)
from ditto.tests.api_server.conftest import make_api_server_config


def _installed_payload() -> dict[str, object]:
    registry = installed_confirmation_verification_profiles()
    assert len(registry) == 1
    profile = next(iter(registry.values()))
    return {**profile.payload(), "checksum": profile.checksum()}


def test_installed_profile_is_exact_bounded_shadow_contract() -> None:
    registry = installed_confirmation_verification_profiles()
    assert len(registry) == 1
    ((identity, profile),) = registry.items()
    assert identity == (profile.revision, profile.checksum())
    assert "shadow" in profile.revision
    assert profile.longmem_cases_per_capability == 8
    assert profile.ablation_coordinator_policy.sample_size == 4
    assert profile.composite.base_weight_bps == 7_000
    assert profile.composite.longmem_weight_bps == 3_000
    assert {lane.lane for lane in profile.provider_lanes} == {"reader", "judge"}
    assert profile.embedding_lane.provider == "perplexity"


def test_installed_profile_covers_starter_turn_and_aggregate_lane_maxima() -> None:
    profile = next(iter(installed_confirmation_verification_profiles().values()))
    reader = next(lane for lane in profile.provider_lanes if lane.lane == "reader")
    selected_cases = profile.longmem_cases_per_capability * len(CAPABILITY_ORDER)
    assert reader.max_requests >= selected_cases * 24

    root = Path(__file__).resolve().parents[5]
    launch = json.loads(
        (
            root
            / "packages"
            / "ditto-screening-protocol"
            / "ditto_screening_protocol"
            / "data"
            / "confirmation_launch_manifest_v9_shadow.json"
        ).read_text()
    )
    required_requests = profile.embedding_lane.max_requests + sum(
        lane.max_requests for lane in profile.provider_lanes
    )
    required_tokens = profile.embedding_lane.max_input_tokens + sum(
        lane.max_total_tokens for lane in profile.provider_lanes
    )
    assert launch["issuance_caps"]["requests_per_bundle"] >= required_requests
    assert launch["issuance_caps"]["tokens_per_bundle"] >= required_tokens

    assert confirmation_inference_cap_requirements(profile) == (
        required_requests,
        required_tokens,
    )
    validate_confirmation_inference_caps(
        profile,
        request_cap=required_requests,
        token_cap=required_tokens,
    )
    with pytest.raises(ConfirmationEvidenceError, match="cannot fund"):
        validate_confirmation_inference_caps(
            profile,
            request_cap=required_requests - 1,
            token_cap=required_tokens,
        )
    with pytest.raises(ConfirmationEvidenceError, match="cannot fund"):
        validate_confirmation_inference_caps(
            profile,
            request_cap=required_requests,
            token_cap=required_tokens - 1,
        )


def test_factory_registers_only_the_exact_release_profile() -> None:
    app = create_api_server(make_api_server_config())
    assert app.state.confirmation_verification_profiles == (
        installed_confirmation_verification_profiles()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(checksum="0" * 64),
        lambda payload: payload["provider_lanes"][0].update(max_requests=49),
        lambda payload: payload["composite"].update(checksum="0" * 64),
    ],
)
def test_decoder_rejects_drift_and_bad_checksums(
    mutate,
) -> None:
    payload = _installed_payload()
    mutate(payload)
    with pytest.raises(ConfirmationProfileInstallationError):
        decode_confirmation_verification_profile(json.dumps(payload).encode())


def test_decoder_ignores_unknown_secret_shaped_fields() -> None:
    payload = _installed_payload()
    payload["provider_api_key"] = "must-not-become-authoritative"

    decoded = decode_confirmation_verification_profile(json.dumps(payload).encode())
    expected = decode_confirmation_verification_profile(
        json.dumps(_installed_payload()).encode()
    )

    assert decoded == expected
    assert "provider_api_key" not in decoded.payload()

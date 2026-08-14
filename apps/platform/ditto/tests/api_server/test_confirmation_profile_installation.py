"""Release-asset tests for the production Bench v9 confirmation profile."""

from __future__ import annotations

import json

import pytest

from ditto.api_server import create_api_server
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
    assert profile.longmem_cases_per_capability == 2
    assert profile.ablation_coordinator_policy.sample_size == 4
    assert profile.composite.base_weight_bps == 7_000
    assert profile.composite.longmem_weight_bps == 3_000
    assert {lane.lane for lane in profile.provider_lanes} == {"reader", "judge"}
    assert profile.embedding_lane.provider == "perplexity"


def test_factory_registers_only_the_exact_release_profile() -> None:
    app = create_api_server(make_api_server_config())
    assert app.state.confirmation_verification_profiles == (
        installed_confirmation_verification_profiles()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(checksum="0" * 64),
        lambda payload: payload.update(provider_api_key="forbidden"),
        lambda payload: payload["provider_lanes"][0].update(max_requests=49),
        lambda payload: payload["composite"].update(checksum="0" * 64),
    ],
)
def test_decoder_rejects_drift_unknown_secret_fields_and_bad_checksums(
    mutate,
) -> None:
    payload = _installed_payload()
    mutate(payload)
    with pytest.raises(ConfirmationProfileInstallationError):
        decode_confirmation_verification_profile(json.dumps(payload).encode())

"""Runtime capability derivation is conservative and descriptor-bound."""

from __future__ import annotations

import pytest

from ditto.validator.stack_identity import validator_capabilities_and_stack

_DIGEST = "sha256:" + "12" * 32
_REF = "ghcr.io/ditto-assistant/example@" + _DIGEST
_COMPONENTS = (
    "DITTO_SUBNET",
    "DITTOBENCH_API",
    "SANDBOX_DOCKER",
    "MODEL_RELAY",
    "PYLON",
    "OLLAMA",
)


def test_source_stack_defaults_to_prefer_screened_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    monkeypatch.setenv("VALIDATOR_SCREENED_IMAGES", "1")
    capabilities, stack = validator_capabilities_and_stack()
    assert stack.mode == "source"
    assert capabilities.screened_images is True
    assert capabilities.require_screened_image is False
    assert capabilities.source_build_fallback is True
    assert capabilities.full_stack_managed is False


def test_source_checkout_revision_never_claims_descriptor_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    revision = "ab" * 20
    monkeypatch.setenv(
        "VALIDATOR_STACK_COMPONENT_DITTO_SUBNET", f"local-source:{revision}"
    )
    monkeypatch.setenv(
        "VALIDATOR_STACK_COMPONENT_SANDBOX_DOCKER", f"local-source:{revision}"
    )
    _, stack = validator_capabilities_and_stack()
    assert stack.components.ditto_subnet.source_revision == revision
    assert stack.components.ditto_subnet.provenance == "local_unverified"
    assert stack.components.sandbox_docker.provenance == "local_unverified"

    monkeypatch.setenv("VALIDATOR_STACK_COMPONENT_DITTOBENCH_API", f"source:{revision}")
    _, pinned_stack = validator_capabilities_and_stack()
    assert pinned_stack.components.dittobench_api.provenance == "committed_pin"


def test_shared_requirement_switches_to_screened_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALIDATOR_REQUIRE_SCREENED_IMAGE", "1")
    monkeypatch.setenv("VALIDATOR_SCREENED_IMAGES", "1")
    capabilities, _ = validator_capabilities_and_stack()
    assert capabilities.require_screened_image is True
    assert capabilities.source_build_fallback is False


def test_managed_descriptor_env_produces_only_signed_exact_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "managed")
    monkeypatch.setenv(
        "VALIDATOR_STACK_DESCRIPTOR_REF",
        "ghcr.io/ditto-assistant/ditto-subnet-stack@" + _DIGEST,
    )
    monkeypatch.setenv("VALIDATOR_STACK_VERSION", "1.2.3")
    monkeypatch.setenv("VALIDATOR_STACK_REVISION", "ab" * 20)
    monkeypatch.setenv("VALIDATOR_STACK_DITTOBENCH_REVISION", "cd" * 20)
    monkeypatch.setenv("VALIDATOR_STACK_COMPOSE_SCHEMA", "1")
    monkeypatch.setenv("VALIDATOR_STACK_UPDATE_PROTOCOL", "1")
    monkeypatch.setenv("VALIDATOR_STACK_UPDATER", "1")
    for component in _COMPONENTS:
        monkeypatch.setenv(f"VALIDATOR_STACK_COMPONENT_{component}", _REF)

    capabilities, stack = validator_capabilities_and_stack()
    assert stack.mode == "managed"
    assert stack.release_descriptor_digest == _DIGEST
    assert capabilities.full_stack_managed is True
    assert capabilities.stack_updater is True
    assert all(
        identity.provenance == "signed_descriptor"
        for identity in stack.components.__dict__.values()
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VALIDATOR_STACK_DESCRIPTOR_REF", "ghcr.io/example:latest"),
        ("VALIDATOR_STACK_COMPONENT_OLLAMA", "docker.io/ollama/ollama:latest"),
        ("VALIDATOR_STACK_REVISION", "main"),
    ],
)
def test_malformed_managed_state_fails_instead_of_claiming_source(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "managed")
    monkeypatch.setenv(
        "VALIDATOR_STACK_DESCRIPTOR_REF",
        "ghcr.io/ditto-assistant/ditto-subnet-stack@" + _DIGEST,
    )
    monkeypatch.setenv("VALIDATOR_STACK_VERSION", "1.2.3")
    monkeypatch.setenv("VALIDATOR_STACK_REVISION", "ab" * 20)
    monkeypatch.setenv("VALIDATOR_STACK_DITTOBENCH_REVISION", "cd" * 20)
    monkeypatch.setenv("VALIDATOR_STACK_COMPOSE_SCHEMA", "1")
    monkeypatch.setenv("VALIDATOR_STACK_UPDATE_PROTOCOL", "1")
    for component in _COMPONENTS:
        monkeypatch.setenv(f"VALIDATOR_STACK_COMPONENT_{component}", _REF)
    monkeypatch.setenv(name, value)

    with pytest.raises((KeyError, ValueError)):
        validator_capabilities_and_stack()

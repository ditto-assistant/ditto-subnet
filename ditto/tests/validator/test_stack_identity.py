"""Runtime capability derivation is conservative and descriptor-bound."""

from __future__ import annotations

import pytest

from ditto.api_models.validator_capabilities import ScorerBenchmarkCapability
from ditto.validator.stack_identity import (
    bind_observed_scorer_identity,
    validator_capabilities_and_stack,
)

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


def test_source_stack_requires_screened_image_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Screened images are mandatory and there is no source-build fallback: a
    # validator must never docker-build untrusted miner source. The requirement
    # is unconditional and not gated on an env toggle.
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    monkeypatch.delenv("VALIDATOR_SCREENED_IMAGES", raising=False)
    capabilities, stack = validator_capabilities_and_stack()
    assert stack.mode == "source"
    assert capabilities.screened_images is True
    assert capabilities.require_screened_image is True
    assert capabilities.source_build_fallback is False
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


def test_source_stack_binds_version_observed_at_matching_pinned_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "ab" * 20
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    monkeypatch.setenv("VALIDATOR_STACK_COMPONENT_DITTOBENCH_API", f"source:{revision}")
    _, stack = validator_capabilities_and_stack()
    scorer = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 3),
        observed_at=1,
        software_version="source-build",
        source_revision=revision,
    )

    bound = bind_observed_scorer_identity(stack, scorer)

    assert bound.components.dittobench_api.version == "source-build"
    assert bound.components.dittobench_api.source_revision == revision
    assert bound.components.dittobench_api.provenance == "committed_pin"


def test_source_stack_does_not_bind_unverified_or_mismatched_scorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "ab" * 20
    monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
    monkeypatch.setenv("VALIDATOR_STACK_COMPONENT_DITTOBENCH_API", f"source:{revision}")
    _, stack = validator_capabilities_and_stack()
    scorer = ScorerBenchmarkCapability(
        status="identity_mismatch",
        supported_bench_versions=(2,),
        observed_at=1,
        software_version="source-build",
        source_revision="cd" * 20,
    )

    assert bind_observed_scorer_identity(stack, scorer) == stack


def test_screened_image_requirement_is_unconditional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The requirement is hardcoded on: neither an unset nor a falsey legacy
    # global env can turn off screened images or re-enable the source-build
    # fallback on a validator.
    for value in ("0", "1"):
        monkeypatch.setenv("VALIDATOR_REQUIRE_SCREENED_IMAGE", value)
        monkeypatch.setenv("VALIDATOR_SCREENED_IMAGES", value)
        capabilities, _ = validator_capabilities_and_stack()
        assert capabilities.screened_images is True
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

"""Schema tests for the signed per-component stack health (heartbeat v9).

The schema is a public, signed wire surface: it must stay closed (no
host-shaped extras), bounded, and internally consistent — an ``unknown``
component cannot smuggle observations, an ``identity_mismatch`` must show the
mismatching observation, and an observed identity is evidence, never a copied
configured pin.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ditto.api_models.stack_health import (
    ObservedComponentIdentity,
    ValidatorComponentHealth,
    ValidatorStackHealth,
    validator_stack_health_signing_token,
)

_REV = "ab" * 20


def _healthy(observed_at: int = 1_784_020_800) -> ValidatorComponentHealth:
    return ValidatorComponentHealth(
        health="healthy", required=True, observed_at=observed_at, ready=True
    )


def _unknown() -> ValidatorComponentHealth:
    return ValidatorComponentHealth(health="unknown", required=True)


def _stack_health(**overrides: ValidatorComponentHealth) -> ValidatorStackHealth:
    components: dict[str, ValidatorComponentHealth] = {
        "ditto_subnet": ValidatorComponentHealth(
            health="healthy",
            required=True,
            observed_at=1_784_020_800,
            ready=True,
            observed_identity=ObservedComponentIdentity(version="1.2.3"),
        ),
        "dittobench_api": _healthy(),
        "sandbox_docker": _unknown(),
        "model_relay": _unknown(),
        "pylon": _healthy(),
        "ollama": _healthy(),
    }
    components.update(overrides)
    return ValidatorStackHealth(**components)


class TestObservedComponentIdentity:
    def test_requires_at_least_one_observation(self) -> None:
        with pytest.raises(ValidationError, match="at least one observed field"):
            ObservedComponentIdentity()

    def test_rejects_off_pattern_values(self) -> None:
        with pytest.raises(ValidationError):
            ObservedComponentIdentity(source_revision="not-a-revision")
        with pytest.raises(ValidationError):
            ObservedComponentIdentity(image_digest="sha256:short")


class TestValidatorComponentHealth:
    def test_unknown_cannot_carry_observations(self) -> None:
        for observation in (
            {"observed_at": 1},
            {"ready": True},
            {"model_ready": False},
            {"observed_identity": ObservedComponentIdentity(version="1.0")},
        ):
            with pytest.raises(ValidationError, match="cannot carry observations"):
                ValidatorComponentHealth(
                    health="unknown",
                    required=True,
                    **observation,  # type: ignore[arg-type]
                )

    def test_observed_states_require_observed_at(self) -> None:
        for health in ("healthy", "degraded", "unreachable"):
            with pytest.raises(ValidationError, match="requires observed_at"):
                ValidatorComponentHealth(health=health, required=True)

    def test_unreachable_cannot_claim_readiness_or_identity(self) -> None:
        with pytest.raises(ValidationError, match="unreachable"):
            ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=1, ready=True
            )
        with pytest.raises(ValidationError, match="unreachable"):
            ValidatorComponentHealth(
                health="unreachable",
                required=True,
                observed_at=1,
                observed_identity=ObservedComponentIdentity(version="1.0"),
            )

    def test_identity_mismatch_requires_the_observation(self) -> None:
        with pytest.raises(ValidationError, match="mismatching observed identity"):
            ValidatorComponentHealth(
                health="identity_mismatch", required=True, observed_at=1, ready=True
            )

    def test_healthy_cannot_be_unready(self) -> None:
        with pytest.raises(ValidationError, match="unready"):
            ValidatorComponentHealth(
                health="healthy", required=True, observed_at=1, ready=False
            )

    def test_schema_is_closed_against_host_shaped_fields(self) -> None:
        # The privacy regression: none of the forbidden host/container/network
        # identifiers can ride the public payload, whatever their key.
        for forbidden in (
            {"container_id": "abc123"},
            {"container_name": "ditto-subnet-1"},
            {"hostname": "validator-vm"},
            {"ip_address": "10.0.0.7"},
            {"internal_url": "http://model-relay:8080"},
            {"socket_path": "/var/run/docker.sock"},
            {"env": {"OPENROUTER_API_KEY": "sk-x"}},
            {"logs": ["line"]},
        ):
            with pytest.raises(ValidationError):
                ValidatorComponentHealth(
                    health="healthy",
                    required=True,
                    observed_at=1,
                    ready=True,
                    **forbidden,  # type: ignore[arg-type]
                )


class TestValidatorStackHealth:
    def test_exactly_six_components_no_extras(self) -> None:
        health = _stack_health()
        assert set(ValidatorStackHealth.model_fields) == {
            "ditto_subnet",
            "dittobench_api",
            "sandbox_docker",
            "model_relay",
            "pylon",
            "ollama",
        }
        with pytest.raises(ValidationError):
            ValidatorStackHealth(
                **dict(health),
                prometheus=_healthy(),  # type: ignore[call-arg]
            )

    def test_reporter_must_observe_itself(self) -> None:
        with pytest.raises(ValidationError, match="always required"):
            _stack_health(
                ditto_subnet=ValidatorComponentHealth(
                    health="healthy", required=False, observed_at=1, ready=True
                )
            )
        with pytest.raises(ValidationError, match="unreachable or unknown"):
            _stack_health(ditto_subnet=_unknown())

    def test_partial_stack_mixes_states(self) -> None:
        # A partial stack — some components probed, some not, one broken — is a
        # valid, reportable observation; validity never implies eligibility.
        health = _stack_health(
            ollama=ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=5
            ),
            model_relay=_unknown(),
        )
        assert health.ollama.health == "unreachable"
        assert health.model_relay.health == "unknown"

    def test_serialized_payload_contains_no_network_shapes(self) -> None:
        payload = json.dumps(_stack_health().model_dump(mode="json"))
        for needle in ("://", "docker.sock", "hostname", "container_"):
            assert needle not in payload


class TestSigningToken:
    def test_token_is_length_prefixed_canonical_json(self) -> None:
        token = validator_stack_health_signing_token(_stack_health())
        length, payload = token.split(":", 1)
        assert int(length) == len(payload.encode())
        decoded = json.loads(payload)
        assert list(decoded) == sorted(decoded)
        # exclude_none: an unknown component serializes to its minimal form.
        assert decoded["sandbox_docker"] == {"health": "unknown", "required": True}

    def test_token_changes_with_any_component_state(self) -> None:
        base = validator_stack_health_signing_token(_stack_health())
        flipped = validator_stack_health_signing_token(
            _stack_health(
                pylon=ValidatorComponentHealth(
                    health="degraded",
                    required=True,
                    observed_at=1_784_020_800,
                    ready=False,
                )
            )
        )
        assert base != flipped

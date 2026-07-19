"""Validation boundaries for the fixed heartbeat protocol-v7 contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import ValidatorHeartbeatRequest
from ditto.api_models.validator_capabilities import (
    ComponentProvenance,
    ValidatorCapabilities,
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)

_DIGEST = "sha256:" + "12" * 32
_REVISION = "ab" * 20
_V7_VECTOR = Path(__file__).parents[1] / "contract/validator_heartbeat_v7.json"


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

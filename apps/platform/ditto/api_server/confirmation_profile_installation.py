"""Load the exact public Bench v9 confirmation profile shipped by release.

This module deliberately has no environment or Secret Manager input.  The
profile is public release data; provider authority is minted later as three
short-lived capabilities attached to one signed validator lease.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from types import MappingProxyType

from ditto.api_server.confirmation_evidence import (
    AblationCoordinatorPolicy,
    AblationVerificationPolicy,
    CompositeVerificationPolicy,
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    EmbeddingLanePolicy,
    ProviderLanePolicy,
    SyntheticBudgetPolicy,
)
from ditto_screening_protocol.confirmation_transport import (
    ConfirmationExecutionProfile,
)

_PROFILE_RESOURCE = "confirmation_execution_profile_v9_shadow.json"


class ConfirmationProfileInstallationError(ValueError):
    """The release-bundled confirmation profile is missing or contradictory."""


def decode_confirmation_verification_profile(
    raw: bytes,
) -> ConfirmationVerificationProfile:
    """Strictly decode one wire profile into Platform's verification policy."""
    try:
        wire = ConfirmationExecutionProfile.model_validate_json(raw, strict=True)
        provider_lanes = tuple(
            ProviderLanePolicy(**lane.model_dump(mode="python"))
            for lane in wire.provider_lanes
        )
        embedding_lane = EmbeddingLanePolicy(
            **wire.embedding_lane.model_dump(mode="python")
        )
        coordinator = AblationCoordinatorPolicy(
            **wire.ablation_coordinator_policy.model_dump(mode="python")
        )

        def ablation_policy(value) -> AblationVerificationPolicy:
            payload = value.model_dump(mode="python")
            return AblationVerificationPolicy(
                intervention=payload["intervention"],
                contract_version=payload["contract_version"],
                threshold_micros=payload["threshold_micros"],
                budget=SyntheticBudgetPolicy(**payload["budget"]),
            )

        composite_payload = wire.composite.model_dump(mode="python")
        composite_checksum = composite_payload.pop("checksum")
        composite = CompositeVerificationPolicy(**composite_payload)
        if composite.checksum() != composite_checksum:
            raise ConfirmationProfileInstallationError(
                "confirmation composite checksum mismatch"
            )
        profile = ConfirmationVerificationProfile(
            schema_version=wire.schema_version,
            revision=wire.revision,
            longmem_profile_revision=wire.longmem_profile_revision,
            longmem_profile_checksum=wire.longmem_profile_checksum,
            longmem_dataset_revision=wire.longmem_dataset_revision,
            longmem_dataset_sha256=wire.longmem_dataset_sha256,
            longmem_selector_revision=wire.longmem_selector_revision,
            longmem_selection_seed=wire.longmem_selection_seed,
            longmem_cases_per_capability=wire.longmem_cases_per_capability,
            longmem_seed_batch_pairs=wire.longmem_seed_batch_pairs,
            longmem_projection_key_sha256=wire.longmem_projection_key_sha256,
            provider_lanes=provider_lanes,
            embedding_lane=embedding_lane,
            ablation_profile_revision=wire.ablation_profile_revision,
            ablation_profile_checksum=wire.ablation_profile_checksum,
            ablation_dataset_sha256=wire.ablation_dataset_sha256,
            ablation_threshold_manifest_sha256=(
                wire.ablation_threshold_manifest_sha256
            ),
            ablation_selection_key_sha256=wire.ablation_selection_key_sha256,
            ablation_projection_key_sha256=wire.ablation_projection_key_sha256,
            ablation_coordinator_policy=coordinator,
            inference_ablation=ablation_policy(wire.inference_ablation),
            embedding_ablation=ablation_policy(wire.embedding_ablation),
            composite=composite,
        )
        profile.validate()
        if profile.checksum() != wire.checksum:
            raise ConfirmationProfileInstallationError(
                "confirmation execution profile checksum mismatch"
            )
        if profile.payload() != wire.model_dump(mode="python", exclude={"checksum"}):
            raise ConfirmationProfileInstallationError(
                "confirmation execution profile changed while decoding"
            )
        return profile
    except ConfirmationProfileInstallationError:
        raise
    except (ConfirmationEvidenceError, ValueError) as error:
        raise ConfirmationProfileInstallationError(
            "confirmation execution profile is invalid"
        ) from error


def installed_confirmation_verification_profiles() -> Mapping[
    tuple[str, str], ConfirmationVerificationProfile
]:
    """Return the immutable registry supported by this Platform release."""
    try:
        raw = (
            files("ditto_screening_protocol.data")
            .joinpath(_PROFILE_RESOURCE)
            .read_bytes()
        )
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise ConfirmationProfileInstallationError(
            "confirmation execution profile release asset is unavailable"
        ) from error
    profile = decode_confirmation_verification_profile(raw)
    return MappingProxyType({(profile.revision, profile.checksum()): profile})


__all__ = [
    "ConfirmationProfileInstallationError",
    "decode_confirmation_verification_profile",
    "installed_confirmation_verification_profiles",
]

"""Load the exact public Bench v9 confirmation profile shipped by release.

This module deliberately has no environment or Secret Manager input.  The
profile is public release data; provider authority is minted later as three
short-lived capabilities attached to one signed validator lease.
"""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatch
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
_PROFILE_RESOURCE_GLOB = "confirmation_execution_profile_v*_shadow.json"


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
            bench_version=wire.bench_version,
            longmem_min_history_sessions=wire.longmem_min_history_sessions,
            longmem_min_history_bytes=wire.longmem_min_history_bytes,
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
    """Return the immutable registry supported by this Platform release.

    Every ``confirmation_execution_profile_v*_shadow.json`` shipped in the
    protocol data package is installed, keyed by its own (revision, checksum).
    One epoch per release asset: shipping a later epoch's lane is dropping in
    its reviewed asset, not editing this module. The v9 asset is required, so a
    release that loses it fails loudly instead of serving an empty registry.
    """
    resources = files("ditto_screening_protocol.data")
    try:
        names = sorted(
            entry.name
            for entry in resources.iterdir()
            if entry.is_file() and fnmatch(entry.name, _PROFILE_RESOURCE_GLOB)
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
        raise ConfirmationProfileInstallationError(
            "confirmation execution profile release asset is unavailable"
        ) from error
    if _PROFILE_RESOURCE not in names:
        raise ConfirmationProfileInstallationError(
            "confirmation execution profile release asset is unavailable"
        )
    registry: dict[tuple[str, str], ConfirmationVerificationProfile] = {}
    for name in names:
        profile = decode_confirmation_verification_profile(
            resources.joinpath(name).read_bytes()
        )
        key = (profile.revision, profile.checksum())
        if key in registry:
            raise ConfirmationProfileInstallationError(
                "confirmation execution profile identity is installed twice"
            )
        registry[key] = profile
    return MappingProxyType(registry)


__all__ = [
    "ConfirmationProfileInstallationError",
    "decode_confirmation_verification_profile",
    "installed_confirmation_verification_profiles",
]

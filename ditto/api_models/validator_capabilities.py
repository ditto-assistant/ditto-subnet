"""Fixed, signed validator execution and stack identity for heartbeat v7."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,63}$"
_PROFILE_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+/-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

ComponentProvenance = Literal["signed_descriptor", "committed_pin", "local_unverified"]
ExecutorIsolation = Literal[
    "unknown", "privileged_dind", "rootless_host", "ephemeral_vm"
]
StackMode = Literal["source", "managed"]
ScorerBenchmarkStatus = Literal[
    "fresh_verified", "legacy_v2", "unreachable", "identity_mismatch"
]
SupportedBenchVersions = Annotated[
    tuple[Annotated[int, Field(ge=1)], ...],
    BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value),
]


class InferenceCalibrationRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: Annotated[str, Field(min_length=1, max_length=120)]
    profile_revision: Annotated[str, Field(pattern=_PROFILE_PATTERN)]
    model: Annotated[str, Field(min_length=1, max_length=120)]


class V7InferenceCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    supported_routes: tuple[InferenceCalibrationRoute, ...]

    @model_validator(mode="after")
    def routes_are_nonempty_unique_and_sorted(self) -> V7InferenceCalibration:
        identities = tuple(
            (route.provider, route.profile_revision, route.model)
            for route in self.supported_routes
        )
        if not identities or tuple(sorted(set(identities))) != identities:
            raise ValueError("v7 inference routes must be nonempty, unique, and sorted")
        return self


class ScorerBenchmarkCapability(BaseModel):
    """Identity-bound benchmark support observed from the scorer sidecar."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: ScorerBenchmarkStatus
    supported_bench_versions: SupportedBenchVersions
    observed_at: Annotated[int | None, Field(ge=0)] = None
    software_version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    source_revision: Annotated[str | None, Field(pattern=_REVISION_PATTERN)] = None
    v7_calibration: V7InferenceCalibration | None = None

    @model_validator(mode="after")
    def support_matches_verified_identity(self) -> ScorerBenchmarkCapability:
        versions = self.supported_bench_versions
        if not versions or tuple(sorted(set(versions))) != versions:
            raise ValueError("supported benchmark versions must be unique and sorted")
        if self.status == "fresh_verified":
            if (
                self.observed_at is None
                or self.software_version is None
                or self.source_revision is None
            ):
                raise ValueError(
                    "fresh verified scorer support requires observation and identity"
                )
        elif versions != (2,):
            raise ValueError("unverified scorer states may advertise only benchmark v2")
        # Any post-v2 version must be backed by verified identity. Naming a
        # single version here let e.g. (2, 4) bypass the requirement entirely.
        if any(version > 2 for version in versions) and self.status != "fresh_verified":
            raise ValueError(
                "benchmark versions above v2 require a fresh verified scorer identity"
            )
        if (7 in versions) != (self.v7_calibration is not None):
            raise ValueError("v7 support requires exact inference calibration identity")
        return self


class ValidatorCapabilities(BaseModel):
    """Closed capability set used by the platform to issue compatible work."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    screened_images: bool
    require_screened_image: bool
    source_build_fallback: bool
    full_stack_managed: bool
    stack_updater: bool
    sandbox_egress_restricted: bool
    ticket_inference: bool = Field(default=False, exclude_if=lambda value: not value)
    executor_isolation: ExecutorIsolation
    scorer_benchmarks: ScorerBenchmarkCapability | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def screened_image_flags_are_consistent(self) -> ValidatorCapabilities:
        if self.require_screened_image and not self.screened_images:
            raise ValueError(
                "requiring screened images requires screened image support"
            )
        if self.require_screened_image == self.source_build_fallback:
            raise ValueError(
                "require_screened_image and source_build_fallback must be opposites"
            )
        if self.stack_updater and not self.full_stack_managed:
            raise ValueError("stack updater requires a managed full stack")
        if (
            self.scorer_benchmarks is not None
            and self.scorer_benchmarks.v7_calibration is not None
            and not self.ticket_inference
        ):
            raise ValueError("v7 calibration requires ticket inference")
        return self


class ValidatorComponentIdentity(BaseModel):
    """Bounded identity for one member of the validator Compose stack."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    image_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = None
    source_revision: Annotated[str | None, Field(pattern=_REVISION_PATTERN)] = None
    version: Annotated[str | None, Field(pattern=_VERSION_PATTERN)] = None
    provenance: ComponentProvenance

    @model_validator(mode="after")
    def has_identity(self) -> ValidatorComponentIdentity:
        if (
            self.image_digest is None
            and self.source_revision is None
            and self.version is None
        ):
            raise ValueError(
                "component must provide an image, source, or version identity"
            )
        if self.provenance == "signed_descriptor" and self.image_digest is None:
            raise ValueError("signed descriptor components require an image digest")
        return self


class ValidatorStackComponents(BaseModel):
    """Exactly the validator and five sidecars in the production stack."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ditto_subnet: ValidatorComponentIdentity
    dittobench_api: ValidatorComponentIdentity
    sandbox_docker: ValidatorComponentIdentity
    model_relay: ValidatorComponentIdentity
    pylon: ValidatorComponentIdentity
    ollama: ValidatorComponentIdentity


class ValidatorStackIdentity(BaseModel):
    """Verified release identity, or explicit source-stack identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: StackMode
    compose_schema: Annotated[int, Field(ge=1, le=2**31 - 1)]
    release_descriptor_digest: Annotated[str | None, Field(pattern=_DIGEST_PATTERN)] = (
        None
    )
    components: ValidatorStackComponents

    @model_validator(mode="after")
    def mode_matches_provenance(self) -> ValidatorStackIdentity:
        identities = tuple(self.components.__dict__.values())
        if self.mode == "managed":
            if self.release_descriptor_digest is None:
                raise ValueError("managed stacks require a release descriptor digest")
            if any(item.provenance != "signed_descriptor" for item in identities):
                raise ValueError(
                    "managed stack components must come from signed descriptor"
                )
        else:
            if self.release_descriptor_digest is not None:
                raise ValueError("source stacks cannot claim a release descriptor")
            if any(item.provenance == "signed_descriptor" for item in identities):
                raise ValueError(
                    "source stack components cannot claim signed provenance"
                )
        return self


def validator_identity_signing_token(
    capabilities: ValidatorCapabilities, stack: ValidatorStackIdentity
) -> str:
    """Return one length-prefixed canonical JSON token for heartbeat v7."""
    payload = json.dumps(
        {
            "capabilities": capabilities.model_dump(mode="json", exclude_none=True),
            # Preserve v7's original explicit-null stack encoding. Only the new
            # optional capability is omitted when absent.
            "stack": stack.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{len(payload.encode('utf-8'))}:{payload}"

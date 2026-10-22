"""Sealed V13 metamorphic package contract; no public challenge material.

This module validates the identity, size, and predeclaration of a private
package before an isolated verifier may execute it. It does not score cases,
prove semantic equivalence, or authorize a policy verdict.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = str
_SHA_PATTERN = r"^[0-9a-f]{64}$"
_REQUIRED_CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_PAYLOAD_BYTES = 128 * 1024
_MAX_TOTAL_PAYLOAD_BYTES = 16 * 1024 * 1024


class V13PrivateProfile(BaseModel):
    """Published, immutable statistical design; no hidden seed or case data."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-metamorphic-v2"] = "v13-private-metamorphic-v2"
    policy_version: Literal[13] = 13
    pairs_per_class_per_seed: Literal[10] = 10
    pairs_per_class_total: Literal[20] = 20
    independent_seed_count: Literal[2] = 2
    material_degradation_bps: Literal[1500] = 1500
    confidence_lower_bound_bps: Literal[500] = 500
    clean_control_max_degradation_bps: Literal[500] = 500
    confidence_level_bps: Literal[9500] = 9500
    # Exploratory class comparisons only. The primary is one pooled test.
    multiple_comparison_method: Literal["holm-bonferroni"] = "holm-bonferroni"
    primary_hypothesis: Literal["pooled-paired-ternary-mean"] = (
        "pooled-paired-ternary-mean"
    )
    primary_interval: Literal["one-sided-student-t"] = "one-sided-student-t"

    def checksum(self) -> Sha256:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


V13_PRIVATE_PROFILE = V13PrivateProfile()
V13_PRIVATE_PROFILE_SHA256 = V13_PRIVATE_PROFILE.checksum()


class V13PrivatePair(BaseModel):
    """Opaque references to two sealed payloads, never their contents."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    pair_id: UUID
    seed_commitment: str = Field(pattern=_SHA_PATTERN)
    transformation_class: Literal[
        "field_entity_rename",
        "request_paraphrase",
        "record_reorder_decoy",
        "catalog_reorder_alias",
    ]
    control_sha256: str = Field(pattern=_SHA_PATTERN)
    variant_sha256: str = Field(pattern=_SHA_PATTERN)

    @model_validator(mode="after")
    def different_payloads(self) -> V13PrivatePair:
        if self.control_sha256 == self.variant_sha256:
            raise ValueError("private pair payloads must differ")
        return self


class V13PrivateManifest(BaseModel):
    """Private, content-addressed case inventory held outside the public repo."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-manifest-v1"] = "v13-private-manifest-v1"
    policy_version: Literal[13] = 13
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    generated_at: datetime
    tool_catalog_applicable: bool
    pairs: tuple[V13PrivatePair, ...] = Field(min_length=60, max_length=512)

    @model_validator(mode="after")
    def complete_design(self) -> V13PrivateManifest:
        if len({pair.pair_id for pair in self.pairs}) != len(self.pairs):
            raise ValueError("duplicate private pair identity")
        all_payloads = [
            digest
            for pair in self.pairs
            for digest in (pair.control_sha256, pair.variant_sha256)
        ]
        if len(set(all_payloads)) != len(all_payloads):
            raise ValueError("private pair payloads are not distinct")
        seeds = {pair.seed_commitment for pair in self.pairs}
        if len(seeds) != V13_PRIVATE_PROFILE.independent_seed_count:
            raise ValueError("private package needs two independent seed commitments")
        counts: Counter[tuple[str, str]] = Counter(
            (pair.seed_commitment, pair.transformation_class) for pair in self.pairs
        )
        required_classes: tuple[str, ...] = _REQUIRED_CLASSES
        if self.tool_catalog_applicable:
            required_classes += ("catalog_reorder_alias",)
        for seed in seeds:
            for class_name in required_classes:
                if (
                    counts[(seed, class_name)]
                    < V13_PRIVATE_PROFILE.pairs_per_class_per_seed
                ):
                    raise ValueError("private package lacks per-seed class coverage")
        for class_name in required_classes:
            if sum(counts[(seed, class_name)] for seed in seeds) < (
                V13_PRIVATE_PROFILE.pairs_per_class_total
            ):
                raise ValueError("private package lacks per-class coverage")
        if not self.tool_catalog_applicable and any(
            pair.transformation_class == "catalog_reorder_alias" for pair in self.pairs
        ):
            raise ValueError("catalog applicability conflicts with package cases")
        return self


class ArtifactCommitment(BaseModel):
    """Identity and time sourced from trusted Platform state, not the package."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    committed_at: datetime


class PrivatePackageRegistration(BaseModel):
    """Digest-only registry row issued by a trusted control plane."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    # Optional for older report-only packages. Matched clean-control use
    # requires this trusted group/role link to a pre-randomness start receipt.
    generation_group_id: UUID | None = None
    generation_role: Literal["target", "known_benign"] | None = None
    generation_receipt_sha256: str | None = Field(default=None, pattern=_SHA_PATTERN)
    registered_at: datetime
    registrar_id: str = Field(min_length=1, max_length=120)


class PreparedV13PrivatePackage(BaseModel):
    """Digest-only, identity-bound handoff after the sealed preflight."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    commitment: ArtifactCommitment
    registration: PrivatePackageRegistration
    manifest: V13PrivateManifest


class SealedPackageStore(Protocol):
    """Private-runner-only store. Backroom never receives handles or bytes."""

    async def read_manifest(self, sha256: Sha256) -> bytes: ...

    async def read_payload(self, sha256: Sha256) -> bytes: ...


class TrustedPrivatePackageRegistry(Protocol):
    """Trusted control plane returns a registered commitment, never case bytes."""

    async def get_registration(
        self, agent_id: UUID, attempt_id: UUID
    ) -> PrivatePackageRegistration: ...


class PrivatePackageUnavailable(ValueError):
    """A package cannot be used; callers must keep verification incomplete."""


async def prepare_sealed_v13_package(
    *,
    store: SealedPackageStore,
    commitment: ArtifactCommitment,
    registry: TrustedPrivatePackageRegistry,
) -> PreparedV13PrivatePackage:
    """Verify sealed bytes and ordering without disclosing private values.

    The caller must obtain the commitment and registration from trusted
    Platform state and use a separate isolated runner to execute cases. This
    returns structure only, never a check pass or integrity finding. All
    failure text is challenge-safe.
    """
    try:
        async with asyncio.timeout(30):
            registration = await registry.get_registration(
                commitment.agent_id, commitment.attempt_id
            )
            return await _prepare_registered_package(
                store=store, commitment=commitment, registration=registration
            )
    except PrivatePackageUnavailable:
        raise
    except Exception:
        raise PrivatePackageUnavailable(
            "private package preparation unavailable"
        ) from None


async def _prepare_registered_package(
    *,
    store: SealedPackageStore,
    commitment: ArtifactCommitment,
    registration: PrivatePackageRegistration,
) -> PreparedV13PrivatePackage:
    if (
        registration.agent_id != commitment.agent_id
        or registration.attempt_id != commitment.attempt_id
        or registration.artifact_sha256 != commitment.artifact_sha256
        or registration.image_sha256 != commitment.image_sha256
        or registration.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or registration.registered_at.tzinfo is None
        or commitment.committed_at.tzinfo is None
        or registration.registered_at <= commitment.committed_at
    ):
        raise PrivatePackageUnavailable("private registration identity mismatch")
    try:
        raw = await store.read_manifest(registration.manifest_sha256)
        if (
            len(raw) > _MAX_MANIFEST_BYTES
            or hashlib.sha256(raw).hexdigest() != registration.manifest_sha256
        ):
            raise PrivatePackageUnavailable("private manifest commitment mismatch")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or set(parsed) != set(
            V13PrivateManifest.model_fields
        ):
            raise PrivatePackageUnavailable("private manifest shape mismatch")
        pairs = parsed.get("pairs")
        if not isinstance(pairs, list) or any(
            not isinstance(pair, dict) or set(pair) != set(V13PrivatePair.model_fields)
            for pair in pairs
        ):
            raise PrivatePackageUnavailable("private pair shape mismatch")
        manifest = V13PrivateManifest.model_validate(parsed)
    except PrivatePackageUnavailable:
        raise
    except Exception:
        raise PrivatePackageUnavailable("private manifest unavailable") from None
    if (
        manifest.agent_id != commitment.agent_id
        or manifest.attempt_id != commitment.attempt_id
        or manifest.artifact_sha256 != commitment.artifact_sha256
        or manifest.image_sha256 != commitment.image_sha256
        or manifest.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        or manifest.generated_at.tzinfo is None
        or manifest.generated_at <= commitment.committed_at
        or manifest.generated_at > registration.registered_at
    ):
        raise PrivatePackageUnavailable("private package identity or order mismatch")
    digests = {
        digest
        for pair in manifest.pairs
        for digest in (pair.control_sha256, pair.variant_sha256)
    }
    total_bytes = 0
    for digest in digests:
        try:
            blob = await store.read_payload(digest)
            total_bytes += len(blob)
            if (
                len(blob) > _MAX_PAYLOAD_BYTES
                or total_bytes > _MAX_TOTAL_PAYLOAD_BYTES
                or hashlib.sha256(blob).hexdigest() != digest
            ):
                raise PrivatePackageUnavailable("private payload commitment mismatch")
        except PrivatePackageUnavailable:
            raise
        except Exception:
            raise PrivatePackageUnavailable("private payload unavailable") from None
    return PreparedV13PrivatePackage(
        commitment=commitment, registration=registration, manifest=manifest
    )


class V13PrivateRunSummary(BaseModel):
    """Sanitized runner output; this alone is never a V13 policy decision."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    runner_hotkey: str = Field(min_length=1, max_length=120)
    status: Literal["completed", "failed", "inconclusive"]
    completed_pairs: int = Field(ge=0, le=512)
    evidence_sha256: str = Field(pattern=_SHA_PATTERN)


class IsolatedV13PrivateRunner(Protocol):
    """Implementation must recheck digests, isolate image, and run paired controls."""

    def run(
        self, *, prepared: PreparedV13PrivatePackage, store: SealedPackageStore
    ) -> Awaitable[V13PrivateRunSummary]: ...

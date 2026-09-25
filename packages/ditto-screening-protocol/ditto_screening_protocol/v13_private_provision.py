"""Trusted, post-commit provisioner for sealed V13 private rotations.

The protected blueprint bank is injected at runtime and is never bundled with
this package. This module writes digest-addressed bytes and publishes only a
digest-only registration. It does not execute cases or authorize a verdict.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.v13_private_clean_control import (
    TrustedGenerationGroup,
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE,
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PrivatePackageRegistration,
    V13PrivateManifest,
    V13PrivatePair,
)

PrivateClass = Literal[
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
    "catalog_reorder_alias",
]
_REQUIRED_CLASSES: tuple[PrivateClass, ...] = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)


class PrivateBlueprintPair(BaseModel):
    """Operator-authored, semantics-reviewed pair kept outside the public repo."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    pair_id: UUID
    transformation_class: PrivateClass
    control: bytes = Field(min_length=1, max_length=128 * 1024)
    variant: bytes = Field(min_length=1, max_length=128 * 1024)

    @model_validator(mode="after")
    def distinct_sides(self) -> PrivateBlueprintPair:
        if self.control == self.variant:
            raise ValueError("private blueprint sides must differ")
        return self


class ProtectedBlueprintBank(Protocol):
    """Trusted source of privately authored cases; no public API exposure."""

    async def load(self) -> tuple[PrivateBlueprintPair, ...]: ...


class SealedPackagePublisher(Protocol):
    """Private object store and append-only trusted registration authority."""

    async def put_blob(self, digest: str, content: bytes) -> None: ...

    async def put_hidden_seed(self, commitment: str, seed: bytes) -> None: ...

    async def register(self, registration: PrivatePackageRegistration) -> None: ...


class MatchedPackagePublisher(Protocol):
    """Protected blob writer plus replay-bound append-only role registration."""

    async def put_blob(self, digest: str, content: bytes) -> None: ...

    async def put_hidden_seed(self, commitment: str, seed: bytes) -> None: ...

    async def register_group(
        self, registration: PrivatePackageRegistration, pair_inventory_sha256: str
    ) -> None: ...


class PrivateProvisioningUnavailable(ValueError):
    """The package cannot be registered; verification remains incomplete."""


class MatchedPrivateProvisioningResult(BaseModel):
    """Digest-only result; neither role has a private verification pass."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    target: PrivatePackageRegistration
    known_benign: PrivatePackageRegistration
    pair_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pair_count: int = Field(ge=60, le=512)


async def provision_v13_matched_private_packages(
    *,
    group: TrustedGenerationGroup,
    target: ArtifactCommitment,
    known_benign: ArtifactCommitment,
    bank: ProtectedBlueprintBank,
    publisher: MatchedPackagePublisher,
    registrar_id: str,
    tool_catalog_applicable: bool,
) -> MatchedPrivateProvisioningResult:
    """Publish one post-group hidden inventory for both immutable image roles.

    The group must be read from the trusted Platform registry, after independent
    control approval. Both manifests are stored before either append-only role
    registration. A failed or partial registration has no private pass.
    """
    try:
        async with asyncio.timeout(300):
            if (
                group.replay_id is None
                or group.started_at.tzinfo is None
                or target.committed_at.tzinfo is None
                or known_benign.committed_at.tzinfo is None
                or not max(target.committed_at, known_benign.committed_at)
                < group.started_at
                < datetime.now(UTC)
                or group.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
                or (
                    target.agent_id,
                    target.attempt_id,
                    target.artifact_sha256,
                    target.image_sha256,
                )
                != (
                    group.target_agent_id,
                    group.target_attempt_id,
                    group.target_artifact_sha256,
                    group.target_image_sha256,
                )
                or (
                    known_benign.agent_id,
                    known_benign.attempt_id,
                    known_benign.artifact_sha256,
                    known_benign.image_sha256,
                )
                != (
                    group.control_agent_id,
                    group.control_attempt_id,
                    group.control_artifact_sha256,
                    group.control_image_sha256,
                )
                or target.agent_id == known_benign.agent_id
                or group.target_receipt_sha256
                != compute_v13_generation_role_digest(group, "target")
                or group.control_receipt_sha256
                != compute_v13_generation_role_digest(group, "known_benign")
            ):
                raise PrivateProvisioningUnavailable("trusted generation unavailable")
            required = _REQUIRED_CLASSES + (
                ("catalog_reorder_alias",) if tool_catalog_applicable else ()
            )
            by_class: dict[str, list[PrivateBlueprintPair]] = defaultdict(list)
            seen_ids: set[UUID] = set()
            for item in await bank.load():
                if item.pair_id in seen_ids:
                    raise PrivateProvisioningUnavailable(
                        "private bank identity invalid"
                    )
                seen_ids.add(item.pair_id)
                by_class[item.transformation_class].append(item)
            if any(
                len(by_class[name]) < 2 * V13_PRIVATE_PROFILE.pairs_per_class_per_seed
                for name in required
            ):
                raise PrivateProvisioningUnavailable(
                    "private bank coverage unavailable"
                )
            seeds = (secrets.token_bytes(32), secrets.token_bytes(32))
            if seeds[0] == seeds[1]:
                raise PrivateProvisioningUnavailable("private rotation unavailable")
            pairs: list[V13PrivatePair] = []
            used: set[UUID] = set()
            for seed in seeds:
                seed_commitment = hashlib.sha256(seed).hexdigest()
                await publisher.put_hidden_seed(seed_commitment, seed)
                for name in required:
                    candidates = sorted(
                        by_class[name],
                        key=lambda item: hmac.digest(
                            seed, item.pair_id.bytes, "sha256"
                        ),
                    )
                    selected = [
                        item for item in candidates if item.pair_id not in used
                    ][: V13_PRIVATE_PROFILE.pairs_per_class_per_seed]
                    if len(selected) != V13_PRIVATE_PROFILE.pairs_per_class_per_seed:
                        raise PrivateProvisioningUnavailable(
                            "private rotation coverage unavailable"
                        )
                    for item in selected:
                        used.add(item.pair_id)
                        control_sha = hashlib.sha256(item.control).hexdigest()
                        variant_sha = hashlib.sha256(item.variant).hexdigest()
                        await publisher.put_blob(control_sha, item.control)
                        await publisher.put_blob(variant_sha, item.variant)
                        pairs.append(
                            V13PrivatePair(
                                pair_id=item.pair_id,
                                seed_commitment=seed_commitment,
                                transformation_class=item.transformation_class,
                                control_sha256=control_sha,
                                variant_sha256=variant_sha,
                            )
                        )
            inventory = json.dumps(
                [pair.model_dump(mode="json") for pair in pairs],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            inventory_sha = hashlib.sha256(inventory).hexdigest()
            generated_at = datetime.now(UTC)
            if generated_at <= group.started_at:
                raise PrivateProvisioningUnavailable("private generation order invalid")
            registrations: dict[str, PrivatePackageRegistration] = {}
            for role, commitment, receipt_sha in (
                ("target", target, group.target_receipt_sha256),
                ("known_benign", known_benign, group.control_receipt_sha256),
            ):
                manifest = V13PrivateManifest(
                    agent_id=commitment.agent_id,
                    attempt_id=commitment.attempt_id,
                    artifact_sha256=commitment.artifact_sha256,
                    image_sha256=commitment.image_sha256,
                    profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                    generated_at=generated_at,
                    tool_catalog_applicable=tool_catalog_applicable,
                    pairs=tuple(pairs),
                )
                raw = json.dumps(
                    manifest.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                manifest_sha = hashlib.sha256(raw).hexdigest()
                await publisher.put_blob(manifest_sha, raw)
                registered_at = datetime.now(UTC)
                if registered_at <= generated_at:
                    raise PrivateProvisioningUnavailable(
                        "private registration order invalid"
                    )
                registrations[role] = PrivatePackageRegistration(
                    agent_id=commitment.agent_id,
                    attempt_id=commitment.attempt_id,
                    artifact_sha256=commitment.artifact_sha256,
                    image_sha256=commitment.image_sha256,
                    profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                    manifest_sha256=manifest_sha,
                    generation_group_id=group.group_id,
                    generation_role=cast(Literal["target", "known_benign"], role),
                    generation_receipt_sha256=receipt_sha,
                    registered_at=registered_at,
                    registrar_id=registrar_id,
                )
            await publisher.register_group(registrations["target"], inventory_sha)
            await publisher.register_group(
                registrations["known_benign"], inventory_sha
            )
            return MatchedPrivateProvisioningResult(
                target=registrations["target"],
                known_benign=registrations["known_benign"],
                pair_inventory_sha256=inventory_sha,
                pair_count=len(pairs),
            )
    except PrivateProvisioningUnavailable:
        raise
    except Exception:
        raise PrivateProvisioningUnavailable(
            "matched private provisioning unavailable"
        ) from None


async def provision_v13_private_package(
    *,
    commitment: ArtifactCommitment,
    bank: ProtectedBlueprintBank,
    publisher: SealedPackagePublisher,
    registrar_id: str,
    tool_catalog_applicable: bool,
) -> PrivatePackageRegistration:
    """Select two secret, disjoint rotations after trusted artifact commitment.

    The private bank must have already been semantically reviewed. Registration
    is the final action after every payload, seed, and manifest is stored. No
    partial publication authorizes a package. Errors deliberately omit case
    contents and blueprints. The caller must run in a protected boundary.
    """
    try:
        async with asyncio.timeout(300):
            if commitment.committed_at.tzinfo is None:
                raise PrivateProvisioningUnavailable("artifact commitment unavailable")
            if datetime.now(UTC) <= commitment.committed_at:
                raise PrivateProvisioningUnavailable("artifact commitment unavailable")
            blueprints = await bank.load()
            required = _REQUIRED_CLASSES + (
                ("catalog_reorder_alias",) if tool_catalog_applicable else ()
            )
            by_class: dict[str, list[PrivateBlueprintPair]] = defaultdict(list)
            seen_ids: set[UUID] = set()
            for item in blueprints:
                if item.pair_id in seen_ids:
                    raise PrivateProvisioningUnavailable(
                        "private bank identity invalid"
                    )
                seen_ids.add(item.pair_id)
                by_class[item.transformation_class].append(item)
            if any(
                len(by_class[class_name])
                < 2 * V13_PRIVATE_PROFILE.pairs_per_class_per_seed
                for class_name in required
            ):
                raise PrivateProvisioningUnavailable(
                    "private bank coverage unavailable"
                )
            seeds = (secrets.token_bytes(32), secrets.token_bytes(32))
            if seeds[0] == seeds[1]:
                raise PrivateProvisioningUnavailable("private rotation unavailable")
            pairs: list[V13PrivatePair] = []
            used: set[UUID] = set()
            for seed in seeds:
                seed_commitment = hashlib.sha256(seed).hexdigest()
                await publisher.put_hidden_seed(seed_commitment, seed)
                for class_name in required:
                    candidates = sorted(
                        by_class[class_name],
                        key=lambda item: hmac.digest(
                            seed, item.pair_id.bytes, "sha256"
                        ),
                    )
                    selected = [
                        item for item in candidates if item.pair_id not in used
                    ][: V13_PRIVATE_PROFILE.pairs_per_class_per_seed]
                    if len(selected) != V13_PRIVATE_PROFILE.pairs_per_class_per_seed:
                        raise PrivateProvisioningUnavailable(
                            "private rotation coverage unavailable"
                        )
                    for item in selected:
                        used.add(item.pair_id)
                        control_sha = hashlib.sha256(item.control).hexdigest()
                        variant_sha = hashlib.sha256(item.variant).hexdigest()
                        await publisher.put_blob(control_sha, item.control)
                        await publisher.put_blob(variant_sha, item.variant)
                        pairs.append(
                            V13PrivatePair(
                                pair_id=item.pair_id,
                                seed_commitment=seed_commitment,
                                transformation_class=item.transformation_class,
                                control_sha256=control_sha,
                                variant_sha256=variant_sha,
                            )
                        )
            generated_at = datetime.now(UTC)
            manifest = V13PrivateManifest(
                agent_id=commitment.agent_id,
                attempt_id=commitment.attempt_id,
                artifact_sha256=commitment.artifact_sha256,
                image_sha256=commitment.image_sha256,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                generated_at=generated_at,
                tool_catalog_applicable=tool_catalog_applicable,
                pairs=tuple(pairs),
            )
            raw = json.dumps(
                manifest.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            manifest_sha = hashlib.sha256(raw).hexdigest()
            await publisher.put_blob(manifest_sha, raw)
            registered_at = datetime.now(UTC)
            if registered_at <= generated_at:
                raise PrivateProvisioningUnavailable("private registration unavailable")
            registration = PrivatePackageRegistration(
                agent_id=commitment.agent_id,
                attempt_id=commitment.attempt_id,
                artifact_sha256=commitment.artifact_sha256,
                image_sha256=commitment.image_sha256,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                manifest_sha256=manifest_sha,
                registered_at=registered_at,
                registrar_id=registrar_id,
            )
            await publisher.register(registration)
            return registration
    except PrivateProvisioningUnavailable:
        raise
    except Exception:
        raise PrivateProvisioningUnavailable(
            "private provisioning unavailable"
        ) from None

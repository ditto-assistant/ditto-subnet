"""Bind a known-benign image to the *same* sealed V13 case inventory.

This is a digest-only preflight. A trusted control-plane registry must supply
the known-benign approval, package registrations, and separate append-only
generation-start receipts. Approval may follow an immutable target artifact,
but must precede either private challenge generation. The result neither runs
the images nor establishes a private-check pass or a policy verdict.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_package import (
    ArtifactCommitment,
    PrivatePackageRegistration,
    SealedPackageStore,
    _prepare_registered_package,
)

_SHA_PATTERN = r"^[0-9a-f]{64}$"


class KnownBenignControlApproval(BaseModel):
    """Predeclared image identity returned by an authenticated control plane.

    The registry adapter must verify the signed/auditable approval record
    before returning it; a miner-provided digest is never sufficient.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    approval_id: UUID
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    approved_at: datetime
    approver_id: str = Field(min_length=1, max_length=120)
    approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)


class TrustedGenerationGroup(BaseModel):
    """One DB-timed append-only event binding both roles before case generation.

    The registry adapter must authenticate this row and audit provenance; the
    digest projections below additionally guard against role/group splicing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    group_id: UUID
    target_agent_id: UUID
    target_attempt_id: UUID
    target_artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    target_image_sha256: str = Field(pattern=_SHA_PATTERN)
    control_agent_id: UUID
    control_attempt_id: UUID
    control_artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    control_image_sha256: str = Field(pattern=_SHA_PATTERN)
    approval_id: UUID
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    started_at: datetime
    target_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    control_receipt_sha256: str = Field(pattern=_SHA_PATTERN)


def compute_v13_generation_role_digest(
    group: TrustedGenerationGroup, role: Literal["target", "known_benign"]
) -> str:
    """Canonical digest shared with Platform's append-only group writer."""
    if group.started_at.tzinfo is None:
        raise ValueError("generation group timestamp is not authoritative UTC")
    payload = {
        "revision": "v13-generation-group-v1",
        "group_id": str(group.group_id),
        "role": role,
        "target_agent_id": str(group.target_agent_id),
        "target_attempt_id": str(group.target_attempt_id),
        "target_artifact_sha256": group.target_artifact_sha256,
        "target_image_sha256": group.target_image_sha256,
        "control_agent_id": str(group.control_agent_id),
        "control_attempt_id": str(group.control_attempt_id),
        "control_artifact_sha256": group.control_artifact_sha256,
        "control_image_sha256": group.control_image_sha256,
        "approval_id": str(group.approval_id),
        "profile_sha256": group.profile_sha256,
        "started_at": group.started_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class TrustedKnownBenignRegistry(Protocol):
    """Authenticated registry; callers must not construct approval from a miner."""

    async def get_approval(
        self, agent_id: UUID, attempt_id: UUID
    ) -> KnownBenignControlApproval: ...


class TrustedGenerationRegistry(Protocol):
    """Returns an authenticated immutable group, not a miner manifest clock."""

    async def get_verified_group(self, group_id: UUID) -> TrustedGenerationGroup: ...


class TrustedGroupedPrivatePackageRegistry(Protocol):
    """One registration per generation group and role, including reused control."""

    async def get_group_registration(
        self, group_id: UUID, role: Literal["target", "known_benign"]
    ) -> PrivatePackageRegistration: ...


class V13MatchedCleanControlCommitment(BaseModel):
    """Digest-only same-case commitment, not an execution or policy result."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    group_id: UUID
    target_agent_id: UUID
    target_attempt_id: UUID
    target_artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    target_image_sha256: str = Field(pattern=_SHA_PATTERN)
    target_manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    control_agent_id: UUID
    control_attempt_id: UUID
    control_artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    control_image_sha256: str = Field(pattern=_SHA_PATTERN)
    control_manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    control_approval_id: UUID
    control_approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    target_generation_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    control_generation_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    pair_inventory_sha256: str = Field(pattern=_SHA_PATTERN)
    pair_count: int = Field(ge=60, le=512)


class MatchedCleanControlUnavailable(ValueError):
    """No matched control: private verification remains incomplete."""


async def _prepare_v13_matched_clean_control(
    *,
    group_id: UUID,
    target: ArtifactCommitment,
    control: ArtifactCommitment,
    store: SealedPackageStore,
    packages: TrustedGroupedPrivatePackageRegistry,
    controls: TrustedKnownBenignRegistry,
    generations: TrustedGenerationRegistry,
) -> V13MatchedCleanControlCommitment:
    """Recheck both sealed packages and require one ordered hidden inventory.

    ``target`` and ``control`` commitments must come from trusted Platform
    state. The approval must come from an authenticated known-benign registry,
    never from the miner or the sealed package. The caller must separately
    execute both pinned images, attest their broker ledgers, and compare their
    completed results before asserting any V13 private finding.
    """
    try:
        group = await generations.get_verified_group(group_id)
        target_registration = await packages.get_group_registration(group_id, "target")
        control_registration = await packages.get_group_registration(
            group_id, "known_benign"
        )
        approval = await controls.get_approval(control.agent_id, control.attempt_id)
        if (
            group.group_id != group_id
            or group.target_agent_id != target.agent_id
            or group.target_attempt_id != target.attempt_id
            or group.target_artifact_sha256 != target.artifact_sha256
            or group.target_image_sha256 != target.image_sha256
            or group.control_agent_id != control.agent_id
            or group.control_attempt_id != control.attempt_id
            or group.control_artifact_sha256 != control.artifact_sha256
            or group.control_image_sha256 != control.image_sha256
            or group.approval_id != approval.approval_id
            or group.profile_sha256 != target_registration.profile_sha256
            or group.profile_sha256 != control_registration.profile_sha256
            or group.started_at.tzinfo is None
            or group.target_receipt_sha256
            != compute_v13_generation_role_digest(group, "target")
            or group.control_receipt_sha256
            != compute_v13_generation_role_digest(group, "known_benign")
            or target.agent_id == control.agent_id
            or target.artifact_sha256 == control.artifact_sha256
            or target.image_sha256 == control.image_sha256
            or approval.agent_id != control.agent_id
            or approval.attempt_id != control.attempt_id
            or approval.artifact_sha256 != control.artifact_sha256
            or approval.image_sha256 != control.image_sha256
            or approval.profile_sha256 != control_registration.profile_sha256
            or approval.approved_at.tzinfo is None
            or approval.approved_at <= control.committed_at
            or approval.approved_at >= group.started_at
        ):
            raise MatchedCleanControlUnavailable("clean control identity unavailable")
        target_package = await _prepare_registered_package(
            store=store,
            commitment=target,
            registration=target_registration,
        )
        control_package = await _prepare_registered_package(
            store=store,
            commitment=control,
            registration=control_registration,
        )
        for commitment, registration, package, role, receipt_sha in (
            (
                target,
                target_registration,
                target_package,
                "target",
                group.target_receipt_sha256,
            ),
            (
                control,
                control_registration,
                control_package,
                "known_benign",
                group.control_receipt_sha256,
            ),
        ):
            if (
                registration.generation_group_id != group_id
                or registration.generation_role != role
                or registration.generation_receipt_sha256 != receipt_sha
                or not (
                    commitment.committed_at
                    < group.started_at
                    < package.manifest.generated_at
                    < registration.registered_at
                )
            ):
                raise MatchedCleanControlUnavailable(
                    "trusted generation start or approval order unavailable"
                )
        if (
            target_package.manifest.tool_catalog_applicable
            != control_package.manifest.tool_catalog_applicable
            or target_package.manifest.pairs != control_package.manifest.pairs
            or target_registration.profile_sha256 != control_registration.profile_sha256
        ):
            raise MatchedCleanControlUnavailable("private inventories do not match")
        inventory = json.dumps(
            [pair.model_dump(mode="json") for pair in target_package.manifest.pairs],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return V13MatchedCleanControlCommitment(
            group_id=group_id,
            target_agent_id=target.agent_id,
            target_attempt_id=target.attempt_id,
            target_artifact_sha256=target.artifact_sha256,
            target_image_sha256=target.image_sha256,
            target_manifest_sha256=target_registration.manifest_sha256,
            control_agent_id=control.agent_id,
            control_attempt_id=control.attempt_id,
            control_artifact_sha256=control.artifact_sha256,
            control_image_sha256=control.image_sha256,
            control_manifest_sha256=control_registration.manifest_sha256,
            control_approval_id=approval.approval_id,
            control_approval_receipt_sha256=approval.approval_receipt_sha256,
            target_generation_receipt_sha256=group.target_receipt_sha256,
            control_generation_receipt_sha256=group.control_receipt_sha256,
            profile_sha256=target_registration.profile_sha256,
            pair_inventory_sha256=hashlib.sha256(inventory).hexdigest(),
            pair_count=len(target_package.manifest.pairs),
        )
    except MatchedCleanControlUnavailable:
        raise
    except Exception:
        raise MatchedCleanControlUnavailable(
            "matched clean control preparation unavailable"
        ) from None


async def prepare_v13_matched_clean_control(
    *,
    group_id: UUID,
    target: ArtifactCommitment,
    control: ArtifactCommitment,
    store: SealedPackageStore,
    packages: TrustedGroupedPrivatePackageRegistry,
    controls: TrustedKnownBenignRegistry,
    generations: TrustedGenerationRegistry,
) -> V13MatchedCleanControlCommitment:
    """Bound digest-only preflight; leave private verification incomplete on fault."""
    try:
        async with asyncio.timeout(60):
            return await _prepare_v13_matched_clean_control(
                group_id=group_id,
                target=target,
                control=control,
                store=store,
                packages=packages,
                controls=controls,
                generations=generations,
            )
    except MatchedCleanControlUnavailable:
        raise
    except Exception:
        raise MatchedCleanControlUnavailable(
            "matched clean control preparation unavailable"
        ) from None

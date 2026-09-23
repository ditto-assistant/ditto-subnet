"""Dormant, group-scoped V13 hidden-seed issuer for matched private cases.

Only an isolated private provisioner may wire these protocols. No Platform or
Backroom endpoint exposes the seed material, and a recorded approval or package
registration alone cannot satisfy the authenticated provenance boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_clean_control import (
    KnownBenignControlApproval,
    MatchedCleanControlUnavailable,
    TrustedGenerationRegistry,
    TrustedGenerationGroup,
    TrustedGroupedPrivatePackageRegistry,
    TrustedKnownBenignRegistry,
    compute_v13_generation_role_digest,
    prepare_v13_matched_clean_control,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    SealedPackageStore,
    V13PrivateManifest,
)

_SHA_PATTERN = r"^[0-9a-f]{64}$"
_SEED_BYTES = 32
_SEED_COUNT = 2
_MAX_SEALED_BYTES = 2048
_REVISION = "v13-private-group-seeds-v1"


class SeedGenerationGroup(TrustedGenerationGroup):
    """The approval receipt recorded with the immutable Platform group."""

    approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)


class AuthenticatedKnownBenignApproval(KnownBenignControlApproval):
    """A verified two-person approval projection, never a raw approval row.

    The registry adapter must verify both signed attestations and their exact
    approval/evidence/image binding before returning this type. This module
    intentionally provides no implementation or fallback for that adapter.
    """

    provenance_status: Literal["two_person_authenticated"]
    provenance_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    completed_at: datetime


class VerifiedRoleCommitment(ArtifactCommitment):
    """Platform-checked V13 attempt and verified image, for one group role."""

    policy_version: Literal[13]
    image_verification_receipt_sha256: str = Field(pattern=_SHA_PATTERN)


class TrustedSeedPrerequisiteRegistry(Protocol):
    """Future adapter must authenticate all three independent Platform reads."""

    async def get_verified_group(self, group_id: UUID) -> SeedGenerationGroup: ...

    async def get_verified_role_commitment(
        self, group_id: UUID, role: Literal["target", "known_benign"]
    ) -> VerifiedRoleCommitment: ...

    async def get_authenticated_approval(
        self, approval_id: UUID
    ) -> AuthenticatedKnownBenignApproval: ...


class StoredSeedBundle(BaseModel):
    """Private-store-only record. Never serialize into a public API or log."""

    model_config = ConfigDict(extra="ignore", frozen=True, repr=False)

    revision: Literal["v13-private-group-seeds-v1"] = _REVISION
    group_id: UUID
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    target_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    control_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    provenance_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    seeds_hex: tuple[str, str]


class SealedSeedRecord(BaseModel):
    """Created by the sealed store from its own clock, with committed bytes."""

    model_config = ConfigDict(extra="ignore", frozen=True, repr=False)

    sealed_bytes: bytes = Field(repr=False)
    committed_at: datetime


class AtomicSealedSeedStore(Protocol):
    """Private durable CAS store keyed uniquely by generation group.

    ``create_once`` must call ``new_bundle`` only under its atomic create-only
    boundary, return the winner's exact committed bytes and trusted commit
    time, and never replace or regenerate a committed group. Both role
    generators must read the same group key from this private store.
    """

    async def create_once(
        self, group_id: UUID, new_bundle: Callable[[], bytes]
    ) -> SealedSeedRecord: ...

    async def read(self, group_id: UUID) -> SealedSeedRecord: ...


class V13SeedIssueReceipt(BaseModel):
    """Digest-only output; does not confer a case, score, or review verdict."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    group_id: UUID
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    target_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    control_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    approval_provenance_sha256: str = Field(pattern=_SHA_PATTERN)
    sealed_bundle_sha256: str = Field(pattern=_SHA_PATTERN)
    seed_commitments: tuple[str, str]
    committed_at: datetime


class V13MatchedSeedInventoryReceipt(BaseModel):
    """Digest-only matched inventory and seed proof, never a policy verdict."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    group_id: UUID
    sealed_bundle_sha256: str = Field(pattern=_SHA_PATTERN)
    target_manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    control_manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    pair_inventory_sha256: str = Field(pattern=_SHA_PATTERN)
    pair_count: int = Field(ge=60, le=512)


class V13SeedIssuanceUnavailable(ValueError):
    """Private challenge generation must remain unavailable."""


def _seed_commitment(group_id: UUID, seed: bytes) -> str:
    return hashlib.sha256(
        b"ditto-v13-private-group-seed-v1\0" + group_id.bytes + seed
    ).hexdigest()


def _new_bundle(
    group: SeedGenerationGroup, approval: AuthenticatedKnownBenignApproval
) -> bytes:
    seeds = (secrets.token_bytes(_SEED_BYTES), secrets.token_bytes(_SEED_BYTES))
    if (
        len(seeds[0]) != _SEED_BYTES
        or len(seeds[1]) != _SEED_BYTES
        or seeds[0] == seeds[1]
    ):
        raise V13SeedIssuanceUnavailable("private seed entropy unavailable")
    bundle = StoredSeedBundle(
        group_id=group.group_id,
        profile_sha256=group.profile_sha256,
        target_receipt_sha256=group.target_receipt_sha256,
        control_receipt_sha256=group.control_receipt_sha256,
        approval_receipt_sha256=group.approval_receipt_sha256,
        provenance_receipt_sha256=approval.provenance_receipt_sha256,
        seeds_hex=(seeds[0].hex(), seeds[1].hex()),
    )
    return json.dumps(
        bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def _validate_record(
    group: SeedGenerationGroup,
    approval: AuthenticatedKnownBenignApproval,
    record: SealedSeedRecord,
) -> V13SeedIssueReceipt:
    if (
        record.committed_at.tzinfo is None
        or record.committed_at <= group.started_at
        or not record.sealed_bytes
        or len(record.sealed_bytes) > _MAX_SEALED_BYTES
    ):
        raise V13SeedIssuanceUnavailable("private seed commitment unavailable")
    try:
        raw = json.loads(record.sealed_bytes)
        if not isinstance(raw, dict) or set(raw) != set(StoredSeedBundle.model_fields):
            raise ValueError("invalid bundle shape")
        bundle = StoredSeedBundle.model_validate(raw)
        seeds = tuple(bytes.fromhex(value) for value in bundle.seeds_hex)
    except Exception:
        raise V13SeedIssuanceUnavailable(
            "private seed commitment unavailable"
        ) from None
    if (
        bundle.group_id != group.group_id
        or bundle.profile_sha256 != group.profile_sha256
        or bundle.target_receipt_sha256 != group.target_receipt_sha256
        or bundle.control_receipt_sha256 != group.control_receipt_sha256
        or bundle.approval_receipt_sha256 != group.approval_receipt_sha256
        or bundle.provenance_receipt_sha256 != approval.provenance_receipt_sha256
        or len(seeds) != _SEED_COUNT
        or any(len(seed) != _SEED_BYTES for seed in seeds)
        or any(seed.hex() != value for seed, value in zip(seeds, bundle.seeds_hex))
        or seeds[0] == seeds[1]
    ):
        raise V13SeedIssuanceUnavailable("private seed commitment unavailable")
    return V13SeedIssueReceipt(
        group_id=group.group_id,
        profile_sha256=group.profile_sha256,
        target_receipt_sha256=group.target_receipt_sha256,
        control_receipt_sha256=group.control_receipt_sha256,
        approval_provenance_sha256=approval.provenance_receipt_sha256,
        sealed_bundle_sha256=hashlib.sha256(record.sealed_bytes).hexdigest(),
        seed_commitments=(
            _seed_commitment(group.group_id, seeds[0]),
            _seed_commitment(group.group_id, seeds[1]),
        ),
        committed_at=record.committed_at,
    )


async def issue_v13_hidden_group_seeds(
    *,
    group_id: UUID,
    registry: TrustedSeedPrerequisiteRegistry,
    store: AtomicSealedSeedStore,
) -> V13SeedIssueReceipt:
    """Issue two independent seeds once for the *shared* target/control group.

    There is deliberately no HTTP route, production registry adapter, or
    private-store adapter in this contract. Those integrations must verify the
    #2183 signed two-person approval, exact Platform image receipts, and
    isolated durable CAS behavior before this can run. A registered package or
    legacy approval row does not suffice.
    """
    try:
        async with asyncio.timeout(30):
            group = await registry.get_verified_group(group_id)
            target = await registry.get_verified_role_commitment(group_id, "target")
            control = await registry.get_verified_role_commitment(
                group_id, "known_benign"
            )
            approval = await registry.get_authenticated_approval(group.approval_id)
            if (
                group.group_id != group_id
                or group.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
                or group.started_at.tzinfo is None
                or group.target_receipt_sha256
                != compute_v13_generation_role_digest(group, "target")
                or group.control_receipt_sha256
                != compute_v13_generation_role_digest(group, "known_benign")
                or target.agent_id != group.target_agent_id
                or target.attempt_id != group.target_attempt_id
                or target.artifact_sha256 != group.target_artifact_sha256
                or target.image_sha256 != group.target_image_sha256
                or control.agent_id != group.control_agent_id
                or control.attempt_id != group.control_attempt_id
                or control.artifact_sha256 != group.control_artifact_sha256
                or control.image_sha256 != group.control_image_sha256
                or target.agent_id == control.agent_id
                or target.artifact_sha256 == control.artifact_sha256
                or target.image_sha256 == control.image_sha256
                or target.committed_at.tzinfo is None
                or control.committed_at.tzinfo is None
                or max(target.committed_at, control.committed_at) >= group.started_at
                or approval.approval_id != group.approval_id
                or approval.approval_receipt_sha256 != group.approval_receipt_sha256
                or approval.agent_id != control.agent_id
                or approval.attempt_id != control.attempt_id
                or approval.artifact_sha256 != control.artifact_sha256
                or approval.image_sha256 != control.image_sha256
                or approval.profile_sha256 != group.profile_sha256
                or approval.approved_at.tzinfo is None
                or approval.completed_at.tzinfo is None
                or not (
                    control.committed_at
                    < approval.approved_at
                    <= approval.completed_at
                    < group.started_at
                )
            ):
                raise V13SeedIssuanceUnavailable(
                    "private seed prerequisites unavailable"
                )
            record = await store.create_once(
                group_id, lambda: _new_bundle(group, approval)
            )
            return _validate_record(group, approval, record)
    except V13SeedIssuanceUnavailable:
        raise
    except Exception:
        raise V13SeedIssuanceUnavailable("private seed issuance unavailable") from None

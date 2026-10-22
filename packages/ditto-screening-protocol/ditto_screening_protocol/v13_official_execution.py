"""Typed identities for report-only miner/official-control private execution.

V1 miner replays retain their existing canonical bytes and signing domain.
Official controls have no miner or screening-attempt identity. These models
describe authenticated commitments; constructing one does not grant authority,
verify an image, approve a control, or establish a policy result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.v13_private_execute import PrivatePairCounts
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    SealedPackageStore,
    V13PrivatePair,
    validate_private_inventory,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageId = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Role = Literal["target", "known_benign"]


class MinerSubjectV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Literal["miner"]
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: Sha256
    image_sha256: Sha256
    image_id: ImageId


class OfficialControlSubjectV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Literal["official_control"]
    control_id: UUID
    fixture_id: UUID
    artifact_sha256: Sha256
    image_sha256: Sha256
    image_id: ImageId


ExecutionSubjectV2 = Annotated[
    MinerSubjectV2 | OfficialControlSubjectV2, Field(discriminator="kind")
]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authoritative timestamp requires a timezone")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def subject_digest(subject: ExecutionSubjectV2) -> str:
    """Known-field projection; ignored JSON cannot enter signed authority."""
    return hashlib.sha256(
        b"ditto:v13-private-subject:v2:" + _canonical(subject.model_dump(mode="json"))
    ).hexdigest()


class SubjectCommitmentV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-subject-commitment-v2"] = (
        "v13-private-subject-commitment-v2"
    )
    subject: ExecutionSubjectV2
    committed_at: datetime
    image_verified_at: datetime

    @model_validator(mode="after")
    def authoritative_times(self) -> SubjectCommitmentV2:
        _utc(self.committed_at)
        _utc(self.image_verified_at)
        if self.image_verified_at < self.committed_at:
            raise ValueError("image verification predates subject commitment")
        return self


class OfficialExecutionApprovalV2(BaseModel):
    """Task-scoped approval, including the actual reviewed runtime envelope.

    The authenticated control plane must verify its retained image/control
    approval and independently reviewed evidence before issuing this record.
    An old image/profile-only approval cannot stand in for runtime approval.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    task_id: UUID
    control: OfficialControlSubjectV2
    control_approval_id: UUID
    control_approval_receipt_sha256: Sha256
    profile_sha256: Sha256
    policy_sha256: Sha256
    runtime_configuration_sha256: Sha256
    source_review_sha256: Sha256
    bank_review_sha256: Sha256
    approved_at: datetime

    @model_validator(mode="after")
    def authoritative_time(self) -> OfficialExecutionApprovalV2:
        _utc(self.approved_at)
        return self

    def receipt_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        payload["approved_at"] = _utc(self.approved_at)
        return hashlib.sha256(
            b"ditto:v13-official-execution-approval:v2:" + _canonical(payload)
        ).hexdigest()


class OfficialGenerationGroupV2(BaseModel):
    """Both real subjects and approvals committed before hidden randomness."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-official-generation-group-v2"] = (
        "v13-official-generation-group-v2"
    )
    group_id: UUID
    task_id: UUID
    target: SubjectCommitmentV2
    known_benign: SubjectCommitmentV2
    approval: OfficialExecutionApprovalV2
    started_at: datetime

    @model_validator(mode="after")
    def distinct_ordered_roles(self) -> OfficialGenerationGroupV2:
        if self.target.subject.kind != "miner":
            raise ValueError("target must be a real miner subject")
        if self.known_benign.subject.kind != "official_control":
            raise ValueError("known benign must be an official control subject")
        if (
            self.target.subject.artifact_sha256
            == self.known_benign.subject.artifact_sha256
            or self.target.subject.image_sha256
            == self.known_benign.subject.image_sha256
            or self.target.subject.image_id == self.known_benign.subject.image_id
        ):
            raise ValueError("target and control must be different immutable images")
        if self.approval.task_id != self.task_id or (
            self.approval.control != self.known_benign.subject
        ):
            raise ValueError("official control approval identity mismatch")
        _utc(self.started_at)
        if not (
            self.known_benign.image_verified_at
            < self.approval.approved_at
            < self.started_at
            and self.target.image_verified_at < self.started_at
        ):
            raise ValueError("approval and image commitments must precede generation")
        return self

    def canonical_projection(self):
        payload = self.model_dump(mode="json")
        payload["started_at"] = _utc(self.started_at)
        for name in ("target", "known_benign"):
            commitment = getattr(self, name)
            payload[name]["committed_at"] = _utc(commitment.committed_at)
            payload[name]["image_verified_at"] = _utc(commitment.image_verified_at)
        payload["approval"]["approved_at"] = _utc(self.approval.approved_at)
        return payload

    def role_digest(self, role: Role) -> str:
        if role not in {"target", "known_benign"}:
            raise ValueError("unsupported private execution role")
        payload = self.canonical_projection()
        payload["role"] = role
        return hashlib.sha256(
            b"ditto:v13-official-generation-role:v2:" + _canonical(payload)
        ).hexdigest()


class OfficialPrivateManifestV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-official-private-manifest-v2"] = (
        "v13-official-private-manifest-v2"
    )
    policy_version: Literal[13] = 13
    task_id: UUID
    group_id: UUID
    role: Role
    subject: ExecutionSubjectV2
    generation_receipt_sha256: Sha256
    profile_sha256: Sha256
    generated_at: datetime
    tool_catalog_applicable: bool
    pairs: tuple[V13PrivatePair, ...] = Field(min_length=60, max_length=512)

    @model_validator(mode="after")
    def complete_inventory(self) -> OfficialPrivateManifestV2:
        validate_private_inventory(self.pairs, self.tool_catalog_applicable)
        _utc(self.generated_at)
        if self.profile_sha256 != V13_PRIVATE_PROFILE_SHA256:
            raise ValueError("unsupported private execution profile")
        if (self.role == "target") != (self.subject.kind == "miner"):
            raise ValueError("private manifest role and subject disagree")
        return self

    def inventory_sha256(self) -> str:
        # Both roles use this inventory; their identity-bound manifests differ.
        return hashlib.sha256(
            _canonical([pair.model_dump(mode="json") for pair in self.pairs])
        ).hexdigest()


class OfficialPackageRegistrationV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-official-package-registration-v2"] = (
        "v13-official-package-registration-v2"
    )
    task_id: UUID
    group_id: UUID
    role: Role
    subject: ExecutionSubjectV2
    generation_receipt_sha256: Sha256
    profile_sha256: Sha256
    manifest_sha256: Sha256
    pair_inventory_sha256: Sha256
    registered_at: datetime

    @model_validator(mode="after")
    def authoritative_time(self) -> OfficialPackageRegistrationV2:
        _utc(self.registered_at)
        return self


class PreparedOfficialPackageV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    group: OfficialGenerationGroupV2
    registration: OfficialPackageRegistrationV2
    manifest: OfficialPrivateManifestV2


class OfficialPackageUnavailable(ValueError):
    """No usable private evidence; text never includes protected case data."""


async def _prepare_official_package_v2(
    *,
    group: OfficialGenerationGroupV2,
    registration: OfficialPackageRegistrationV2,
    store: SealedPackageStore,
) -> PreparedOfficialPackageV2:
    """Check an authenticated registration against all sealed input bytes.

    The caller must obtain group/registration from the active trusted lease.
    This check neither authenticates a caller-supplied group nor grants CLEAR.
    """
    try:
        raw = await store.read_manifest(registration.manifest_sha256)
        if len(raw) > 256 * 1024 or (
            hashlib.sha256(raw).hexdigest() != registration.manifest_sha256
        ):
            raise OfficialPackageUnavailable("private manifest commitment mismatch")
        manifest = OfficialPrivateManifestV2.model_validate_json(raw)
        commitment = (
            group.target if registration.role == "target" else group.known_benign
        )
        if (
            registration.task_id != group.task_id
            or registration.group_id != group.group_id
            or registration.subject != commitment.subject
            or registration.generation_receipt_sha256
            != group.role_digest(registration.role)
            or registration.profile_sha256 != group.approval.profile_sha256
            or registration.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
            or manifest.task_id != registration.task_id
            or manifest.group_id != registration.group_id
            or manifest.role != registration.role
            or manifest.subject != registration.subject
            or manifest.profile_sha256 != registration.profile_sha256
            or manifest.generation_receipt_sha256
            != registration.generation_receipt_sha256
            or manifest.inventory_sha256() != registration.pair_inventory_sha256
            or not group.started_at < manifest.generated_at < registration.registered_at
        ):
            raise OfficialPackageUnavailable(
                "private package identity or order mismatch"
            )
        total = 0
        for pair in manifest.pairs:
            for digest in (pair.control_sha256, pair.variant_sha256):
                payload = await store.read_payload(digest)
                total += len(payload)
                if (
                    not 0 < len(payload) <= 128 * 1024
                    or total > 16 * 1024 * 1024
                    or hashlib.sha256(payload).hexdigest() != digest
                ):
                    raise OfficialPackageUnavailable(
                        "private payload commitment mismatch"
                    )
        return PreparedOfficialPackageV2(
            group=group, registration=registration, manifest=manifest
        )
    except OfficialPackageUnavailable:
        raise
    except Exception:
        raise OfficialPackageUnavailable("private package unavailable") from None


async def prepare_official_package_v2(
    *,
    group: OfficialGenerationGroupV2,
    registration: OfficialPackageRegistrationV2,
    store: SealedPackageStore,
) -> PreparedOfficialPackageV2:
    try:
        async with asyncio.timeout(30):
            return await _prepare_official_package_v2(
                group=group, registration=registration, store=store
            )
    except OfficialPackageUnavailable:
        raise
    except Exception:
        raise OfficialPackageUnavailable("private package unavailable") from None


class OfficialInventoryCellV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    transformation_class: Literal[
        "field_entity_rename",
        "request_paraphrase",
        "record_reorder_decoy",
        "catalog_reorder_alias",
    ]
    seed_commitment: Sha256
    pairs: int = Field(ge=10, le=512)


class OfficialMatchedCommitmentV2(BaseModel):
    """Different role manifests, authenticated against one shared inventory."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    group: OfficialGenerationGroupV2
    target_manifest_sha256: Sha256
    control_manifest_sha256: Sha256
    pair_inventory_sha256: Sha256
    pair_count: int = Field(ge=60, le=512)
    tool_catalog_applicable: bool
    target_generated_at: datetime
    control_generated_at: datetime
    target_registered_at: datetime
    control_registered_at: datetime
    inventory_cells: tuple[OfficialInventoryCellV2, ...] = Field(
        min_length=6, max_length=8
    )

    @model_validator(mode="after")
    def complete_ordered_inventory(self) -> OfficialMatchedCommitmentV2:
        for generated, registered in (
            (self.target_generated_at, self.target_registered_at),
            (self.control_generated_at, self.control_registered_at),
        ):
            _utc(generated)
            _utc(registered)
            if not self.group.started_at < generated < registered:
                raise ValueError(
                    "private package generation or registration order mismatch"
                )
        if self.target_manifest_sha256 == self.control_manifest_sha256:
            raise ValueError("role manifests must bind distinct subjects")
        keys = {
            (cell.transformation_class, cell.seed_commitment)
            for cell in self.inventory_cells
        }
        seeds = {cell.seed_commitment for cell in self.inventory_cells}
        classes = {"field_entity_rename", "request_paraphrase", "record_reorder_decoy"}
        if self.tool_catalog_applicable:
            classes.add("catalog_reorder_alias")
        if (
            len(seeds) != 2
            or len(keys) != len(self.inventory_cells)
            or keys != {(kind, seed) for kind in classes for seed in seeds}
            or sum(cell.pairs for cell in self.inventory_cells) != self.pair_count
        ):
            raise ValueError("private matched inventory coverage mismatch")
        return self


def match_official_packages_v2(
    target: PreparedOfficialPackageV2, known_benign: PreparedOfficialPackageV2
) -> OfficialMatchedCommitmentV2:
    """Caller first prepares both roles using authenticated registry state."""
    if (
        target.group != known_benign.group
        or target.manifest.role != "target"
        or known_benign.manifest.role != "known_benign"
        or target.manifest.pairs != known_benign.manifest.pairs
        or target.manifest.tool_catalog_applicable
        != known_benign.manifest.tool_catalog_applicable
        or target.registration.manifest_sha256
        == known_benign.registration.manifest_sha256
        or target.registration.pair_inventory_sha256
        != known_benign.registration.pair_inventory_sha256
    ):
        raise OfficialPackageUnavailable("matched private inventories unavailable")
    return OfficialMatchedCommitmentV2(
        group=target.group,
        target_manifest_sha256=target.registration.manifest_sha256,
        control_manifest_sha256=known_benign.registration.manifest_sha256,
        pair_inventory_sha256=target.registration.pair_inventory_sha256,
        pair_count=len(target.manifest.pairs),
        tool_catalog_applicable=target.manifest.tool_catalog_applicable,
        target_generated_at=target.manifest.generated_at,
        control_generated_at=known_benign.manifest.generated_at,
        target_registered_at=target.registration.registered_at,
        control_registered_at=known_benign.registration.registered_at,
        inventory_cells=tuple(
            OfficialInventoryCellV2(
                transformation_class=kind, seed_commitment=seed, pairs=count
            )
            for (kind, seed), count in sorted(
                Counter(
                    (pair.transformation_class, pair.seed_commitment)
                    for pair in target.manifest.pairs
                ).items()
            )
        ),
    )


class OfficialPrivateRunSummaryV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_id: UUID
    group_id: UUID
    role: Role
    subject: ExecutionSubjectV2
    profile_sha256: Sha256
    manifest_sha256: Sha256
    pair_inventory_sha256: Sha256
    runner_hotkey: str = Field(min_length=1, max_length=120)
    status: Literal["completed", "failed", "inconclusive"]
    completed_pairs: int = Field(ge=0, le=512)
    evidence_sha256: Sha256


class OfficialPrivateResultV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: OfficialPrivateRunSummaryV2
    aggregates: tuple[PrivatePairCounts, ...] = Field(min_length=6, max_length=8)


class OfficialExecutionLeaseBindingV2(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    task_id: UUID
    lease_id: UUID
    process_key_sha256: Sha256
    process_instance_id: str = Field(min_length=1, max_length=160)
    worker_release_sha256: Sha256


class OfficialPrivateReceiptV2(BaseModel):
    """Signed report-only observation; never fills unrelated V13 checks."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-official-private-receipt-v2"] = (
        "v13-official-private-receipt-v2"
    )
    binding: OfficialExecutionLeaseBindingV2
    matched: OfficialMatchedCommitmentV2
    target: OfficialPrivateResultV2
    known_benign: OfficialPrivateResultV2
    runner_hotkey: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @model_validator(mode="after")
    def bound_complete_roles(self) -> OfficialPrivateReceiptV2:
        group = self.matched.group
        _utc(self.observed_at)
        if (
            self.binding.task_id != group.task_id
            or self.observed_at
            <= max(
                self.matched.target_registered_at, self.matched.control_registered_at
            )
            or group.approval.profile_sha256 != V13_PRIVATE_PROFILE_SHA256
        ):
            raise ValueError("private receipt task or time mismatch")
        expected_keys: set[tuple[str, str, int]] | None = None
        for role, manifest_sha in (
            ("target", self.matched.target_manifest_sha256),
            ("known_benign", self.matched.control_manifest_sha256),
        ):
            result = getattr(self, role)
            summary = result.summary
            if (
                summary.task_id != group.task_id
                or summary.group_id != group.group_id
                or summary.role != role
                or summary.subject != getattr(group, role).subject
                or summary.profile_sha256 != group.approval.profile_sha256
                or summary.manifest_sha256 != manifest_sha
                or summary.pair_inventory_sha256 != self.matched.pair_inventory_sha256
                or summary.runner_hotkey != self.runner_hotkey
                or summary.status != "completed"
                or summary.completed_pairs != self.matched.pair_count
            ):
                raise ValueError("private receipt role identity mismatch")
            seeds = {item.seed_commitment for item in result.aggregates}
            classes = {
                "field_entity_rename",
                "request_paraphrase",
                "record_reorder_decoy",
            }
            if self.matched.tool_catalog_applicable:
                classes.add("catalog_reorder_alias")
            keys = {
                (item.transformation_class, item.seed_commitment)
                for item in result.aggregates
            }
            if (
                len(seeds) != 2
                or len(keys) != len(result.aggregates)
                or keys
                != {(class_name, seed) for class_name in classes for seed in seeds}
                or sum(item.pairs for item in result.aggregates)
                != self.matched.pair_count
            ):
                raise ValueError("private receipt aggregate coverage mismatch")
            for item in result.aggregates:
                if not (
                    item.pairs >= 10
                    and 0
                    <= item.control_only_correct
                    <= item.control_correct
                    <= item.pairs
                    and 0
                    <= item.variant_only_correct
                    <= item.variant_correct
                    <= item.pairs
                    and item.control_correct - item.control_only_correct
                    == item.variant_correct - item.variant_only_correct
                    and item.control_correct + item.variant_only_correct <= item.pairs
                ):
                    raise ValueError("private receipt aggregate outcome mismatch")
            inventory = {
                (item.transformation_class, item.seed_commitment, item.pairs)
                for item in result.aggregates
            }
            if inventory != {
                (cell.transformation_class, cell.seed_commitment, cell.pairs)
                for cell in self.matched.inventory_cells
            }:
                raise ValueError(
                    "private receipt aggregates differ from committed inventory"
                )
            if expected_keys is not None and inventory != expected_keys:
                raise ValueError("private receipt role inventories differ")
            expected_keys = inventory
        return self

    def signing_message(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        payload["observed_at"] = _utc(self.observed_at)
        payload["matched"]["group"] = self.matched.group.canonical_projection()
        for field in (
            "target_generated_at",
            "control_generated_at",
            "target_registered_at",
            "control_registered_at",
        ):
            payload["matched"][field] = _utc(getattr(self.matched, field))
        # Group role digests normalize all authoritative nested timestamps.
        payload["generation_target_sha256"] = self.matched.group.role_digest("target")
        payload["generation_control_sha256"] = self.matched.group.role_digest(
            "known_benign"
        )
        return b"ditto:v13-official-private-receipt:v2:\n" + _canonical(payload)


def authentic_official_private_receipt_v2(
    receipt: OfficialPrivateReceiptV2,
    *,
    expected_binding: OfficialExecutionLeaseBindingV2,
    expected_matched: OfficialMatchedCommitmentV2,
    enrolled_runner_hotkey: str,
    source_worker_hotkey: str,
    lease_started_at: datetime,
    lease_deadline: datetime,
    verify_signature: Callable[[str, bytes, str], bool],
) -> bool:
    """Caller supplies current locked DB identities and authenticated lease."""
    if (
        receipt.binding != expected_binding
        or receipt.matched != expected_matched
        or receipt.runner_hotkey != enrolled_runner_hotkey
        or receipt.runner_hotkey == source_worker_hotkey
        or lease_started_at.tzinfo is None
        or lease_deadline.tzinfo is None
        or not lease_started_at <= receipt.observed_at < lease_deadline
    ):
        return False
    return verify_signature(
        receipt.runner_hotkey, receipt.signing_message(), receipt.signature
    )

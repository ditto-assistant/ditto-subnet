"""Authenticated Platform adapter for replay-bound private case inputs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screener.platform import PlatformClient
from ditto_screener.v13_private_adapter import PrivateExecutionUnavailable
from ditto_screener.v13_private_runtime import TrustedPrivateImage
from ditto_screening_protocol.v13_private_clean_control import (
    KnownBenignControlApproval,
    TrustedGenerationGroup,
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_package import (
    ArtifactCommitment,
    PrivatePackageRegistration,
)


class _ImageInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    role: Literal["target", "known_benign"]
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=8 << 30)
    verified_at: datetime
    committed_at: datetime
    url: str = Field(min_length=8, max_length=8192)

    def commitment(self) -> ArtifactCommitment:
        return ArtifactCommitment(
            agent_id=self.agent_id,
            attempt_id=self.attempt_id,
            artifact_sha256=self.artifact_sha256,
            image_sha256=self.image_sha256,
            committed_at=self.committed_at,
        )

    def runtime_image(self) -> TrustedPrivateImage:
        return TrustedPrivateImage(
            role=self.role,
            agent_id=self.agent_id,
            attempt_id=self.attempt_id,
            artifact_sha256=self.artifact_sha256,
            image_sha256=self.image_sha256,
            image_id=self.image_id,
            size_bytes=self.size_bytes,
            url=self.url,
        )


class ReplayPrivateSnapshot(BaseModel):
    """Parsed identities from one current Platform private-inputs response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    replay_id: UUID
    lease_started_at: datetime
    lease_deadline: datetime
    group: TrustedGenerationGroup
    approval: KnownBenignControlApproval
    target_package: PrivatePackageRegistration
    control_package: PrivatePackageRegistration
    target_image: _ImageInput
    control_image: _ImageInput
    pair_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    urls_expire_at: datetime


class PlatformReplayPrivateRegistry:
    """Re-fetches current lease and exact roles before every case launch."""

    def __init__(
        self, platform: PlatformClient, *, replay_id: UUID, group_id: UUID
    ) -> None:
        self.platform = platform
        self.replay_id = replay_id
        self.group_id = group_id

    async def snapshot(self) -> ReplayPrivateSnapshot:
        try:
            raw = await self.platform.replay_private_inputs(self.replay_id)
            group_raw = raw["group"]
            approval_raw = raw["approval"]
            target_raw = raw["target_package"]
            control_raw = raw["control_package"]
            group = TrustedGenerationGroup.model_validate(group_raw)
            approval = KnownBenignControlApproval.model_validate(
                {**approval_raw, "approver_id": approval_raw["actor"]}
            )
            target = PrivatePackageRegistration.model_validate(
                {
                    **target_raw,
                    "generation_group_id": target_raw["group_id"],
                    "generation_role": target_raw["role"],
                    "registrar_id": target_raw["registrar_actor"],
                }
            )
            control = PrivatePackageRegistration.model_validate(
                {
                    **control_raw,
                    "generation_group_id": control_raw["group_id"],
                    "generation_role": control_raw["role"],
                    "registrar_id": control_raw["registrar_actor"],
                }
            )
            snapshot = ReplayPrivateSnapshot(
                replay_id=raw["replay_id"],
                lease_started_at=raw["lease_started_at"],
                lease_deadline=raw["lease_deadline"],
                group=group,
                approval=approval,
                target_package=target,
                control_package=control,
                target_image=_ImageInput.model_validate(raw["target_image"]),
                control_image=_ImageInput.model_validate(raw["control_image"]),
                pair_inventory_sha256=target_raw["pair_inventory_sha256"],
                urls_expire_at=raw["urls_expire_at"],
            )
            if (
                snapshot.replay_id != self.replay_id
                or group.group_id != self.group_id
                or group.replay_id != self.replay_id
                or snapshot.urls_expire_at.tzinfo is None
                or snapshot.lease_deadline.tzinfo is None
                or snapshot.lease_deadline <= datetime.now(UTC)
                or snapshot.urls_expire_at <= datetime.now(UTC) + timedelta(seconds=10)
                or group.target_receipt_sha256
                != compute_v13_generation_role_digest(group, "target")
                or group.control_receipt_sha256
                != compute_v13_generation_role_digest(group, "known_benign")
                or approval.approval_id != group.approval_id
                or approval.approval_receipt_sha256 != group.approval_receipt_sha256
                or target.generation_receipt_sha256 != group.target_receipt_sha256
                or control.generation_receipt_sha256 != group.control_receipt_sha256
                or target_raw["pair_inventory_sha256"]
                != control_raw["pair_inventory_sha256"]
                or snapshot.target_image.role != "target"
                or snapshot.control_image.role != "known_benign"
                or snapshot.target_image.agent_id != group.target_agent_id
                or snapshot.target_image.attempt_id != group.target_attempt_id
                or snapshot.target_image.artifact_sha256 != group.target_artifact_sha256
                or snapshot.target_image.image_sha256 != group.target_image_sha256
                or snapshot.control_image.agent_id != group.control_agent_id
                or snapshot.control_image.attempt_id != group.control_attempt_id
                or snapshot.control_image.artifact_sha256
                != group.control_artifact_sha256
                or snapshot.control_image.image_sha256 != group.control_image_sha256
            ):
                raise PrivateExecutionUnavailable("private Platform binding changed")
            return snapshot
        except PrivateExecutionUnavailable:
            raise
        except Exception:
            raise PrivateExecutionUnavailable(
                "private Platform registry unavailable"
            ) from None

    async def get_verified_group(self, group_id: UUID) -> TrustedGenerationGroup:
        if group_id != self.group_id:
            raise PrivateExecutionUnavailable("private generation group mismatch")
        return (await self.snapshot()).group

    async def get_group_registration(
        self, group_id: UUID, role: Literal["target", "known_benign"]
    ) -> PrivatePackageRegistration:
        if group_id != self.group_id:
            raise PrivateExecutionUnavailable("private generation group mismatch")
        snapshot = await self.snapshot()
        return snapshot.target_package if role == "target" else snapshot.control_package

    async def get_approval(
        self, agent_id: UUID, attempt_id: UUID
    ) -> KnownBenignControlApproval:
        snapshot = await self.snapshot()
        if (
            snapshot.approval.agent_id != agent_id
            or snapshot.approval.attempt_id != attempt_id
        ):
            raise PrivateExecutionUnavailable("known-benign control mismatch")
        return snapshot.approval

    async def resolve(
        self,
        *,
        role: Literal["target", "known_benign"],
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
    ) -> TrustedPrivateImage:
        snapshot = await self.snapshot()
        image = snapshot.target_image if role == "target" else snapshot.control_image
        if (
            image.agent_id != agent_id
            or image.attempt_id != attempt_id
            or image.artifact_sha256 != artifact_sha256
            or image.image_sha256 != image_sha256
        ):
            raise PrivateExecutionUnavailable("private case image changed")
        return image.runtime_image()

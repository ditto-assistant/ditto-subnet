"""Official control identities, separate from miner submissions and attempts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageId = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class OfficialControlImageView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_kind: Literal["canonical_starter_fixture"] = "canonical_starter_fixture"
    control_id: UUID
    request_id: UUID
    fixture_id: UUID
    artifact_sha256: Sha256
    image_sha256: Sha256
    image_id: ImageId
    size_bytes: int
    producer: str
    created_at: datetime
    verified_at: datetime | None
    # Historical archive verification never established executed image identity
    # or semantic verification. Retirement does not upgrade that evidence.
    image_identity_execution_verified: Literal[False] = False
    policy_verification_complete: Literal[False] = False


class OfficialControlImageArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    control: OfficialControlImageView
    download_url: str
    expires_at: datetime


class OfficialControlApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    image_sha256: Sha256
    image_id: ImageId
    profile_sha256: Sha256
    review_evidence_sha256: Sha256
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8)]


class OfficialControlApprovalView(OfficialControlApprovalRequest):
    source_kind: Literal["canonical_starter_fixture"] = "canonical_starter_fixture"
    approval_id: UUID
    control_id: UUID
    fixture_id: UUID
    artifact_sha256: Sha256
    approval_receipt_sha256: Sha256
    actor: str
    approved_at: datetime
    status: Literal["recorded_unverified"] = "recorded_unverified"
    policy_verification_complete: Literal[False] = False

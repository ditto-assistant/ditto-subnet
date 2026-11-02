"""Historical matched-control receipt identity; no registry or case preflight."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SHA_PATTERN = r"^[0-9a-f]{64}$"


class V13MatchedCleanControlCommitment(BaseModel):
    """Digest-only same-case commitment, not an execution or policy result."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    group_id: UUID
    replay_id: UUID | None = None
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

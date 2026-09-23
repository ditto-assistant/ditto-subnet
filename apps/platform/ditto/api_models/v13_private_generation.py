"""Digest-only operator contracts for V13 private challenge generation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SHA = r"^[0-9a-f]{64}$"


class V13KnownBenignApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA)
    image_sha256: str = Field(pattern=_SHA)
    profile_sha256: str = Field(pattern=_SHA)
    review_evidence_sha256: str = Field(pattern=_SHA)
    reason: str = Field(min_length=8)


class V13KnownBenignApprovalView(V13KnownBenignApprovalRequest):
    approval_id: UUID
    approval_receipt_sha256: str = Field(pattern=_SHA)
    actor: str
    approved_at: datetime
    status: Literal["recorded_unverified"] = "recorded_unverified"


class V13KnownBenignAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assertion: str = Field(min_length=16, max_length=2048)
    reason: str = Field(min_length=8)


class V13KnownBenignProvenanceView(BaseModel):
    approval_id: UUID
    review_evidence_sha256: str = Field(pattern=_SHA)
    authenticated_reviewers: int = Field(ge=0, le=2)
    status: Literal[
        "recorded_unverified", "one_authenticated_reviewer", "two_person_authenticated"
    ]
    provenance_receipt_sha256: str | None = Field(default=None, pattern=_SHA)
    completed_at: datetime | None = None


class V13GenerationStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_agent_id: UUID
    target_attempt_id: UUID
    target_artifact_sha256: str = Field(pattern=_SHA)
    target_image_sha256: str = Field(pattern=_SHA)
    approval_id: UUID
    profile_sha256: str = Field(pattern=_SHA)


class V13GenerationGroupView(BaseModel):
    group_id: UUID
    target_agent_id: UUID
    target_attempt_id: UUID
    target_artifact_sha256: str = Field(pattern=_SHA)
    target_image_sha256: str = Field(pattern=_SHA)
    control_agent_id: UUID
    control_attempt_id: UUID
    control_artifact_sha256: str = Field(pattern=_SHA)
    control_image_sha256: str = Field(pattern=_SHA)
    approval_id: UUID
    approval_receipt_sha256: str = Field(pattern=_SHA)
    profile_sha256: str = Field(pattern=_SHA)
    target_receipt_sha256: str = Field(pattern=_SHA)
    control_receipt_sha256: str = Field(pattern=_SHA)
    actor: str
    started_at: datetime
    status: Literal["recorded_unverified"] = "recorded_unverified"

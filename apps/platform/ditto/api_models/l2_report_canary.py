"""Exact-attempt, non-authoritative L2 canary wire contract."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol import ScoredRuntimeEvidenceLease


class L2CanaryScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    # FastAPI parses JSON into Python strings before model validation. Keep the
    # rest of this wire model strict while accepting canonical UUID strings.
    request_id: Annotated[UUID, Field(strict=False)]
    agent_id: Annotated[UUID, Field(strict=False)]
    source_attempt_id: Annotated[UUID, Field(strict=False)]
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Literal[13]
    expected_agent_status: str
    expected_score_count: Annotated[int, Field(ge=0)]
    target_node_id: str
    review_label: Literal["candidate_clear", "known_reject"]
    confirm_report_only: Literal[True]


class L2CanaryView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    canary_id: UUID
    request_id: UUID
    agent_id: UUID
    source_attempt_id: UUID
    artifact_sha256: str
    target_node_id: str
    expected_agent_status: str
    expected_score_count: int
    review_label: str
    status: str
    claimed_instance_id: str | None
    lease_expires_at: datetime | None
    report: dict | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class L2CanaryClaimRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    instance_id: Annotated[str, Field(min_length=1, max_length=63)]
    settings_revision: Annotated[int, Field(ge=0)]
    settings_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class L2CanaryClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    canary_id: UUID
    agent_id: UUID
    source_attempt_id: UUID
    artifact_sha256: str
    bench_version: int
    policy_version: int
    miner_hotkey: str
    lease_token: str
    lease_expires_at: datetime
    download_url: str
    scored_runtime_evidence: ScoredRuntimeEvidenceLease


class L2CanaryCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    lease_token: str
    status: Literal["succeeded", "incomplete"]
    report: dict
    error_code: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    @model_validator(mode="after")
    def bounded_report(self) -> L2CanaryCompleteRequest:
        if len(json.dumps(self.report, separators=(",", ":"))) > 512_000:
            raise ValueError("canary report exceeds 512000 bytes")
        return self


class L2CanaryCompleteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    accepted: bool

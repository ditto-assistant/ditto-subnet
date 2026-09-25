"""Wire models for the non-authoritative screener fan-out shadow lane."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

FanoutShadowStatus = Literal[
    "queued", "leased", "running", "succeeded", "incomplete", "skipped"
]
FanoutShadowOutcome = Literal[
    "no_findings",
    "candidate",
    "unresolved_candidate",
    "critic_also_flagged",
    "incomplete",
    "skipped",
]


class FanoutShadowSourceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_url_b64: str
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Annotated[int, Field(ge=1)]
    policy_manifest_profile: Literal["core", "l1", "l1_l2"]
    policy_manifest_rotation_id: Annotated[str, Field(min_length=1, max_length=80)]
    policy_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FanoutShadowCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    status: Literal["succeeded", "incomplete"]
    outcome: FanoutShadowOutcome
    report: dict
    error_code: Annotated[str, Field(min_length=1, max_length=120)] | None = None

    @model_validator(mode="after")
    def validate_report(self) -> FanoutShadowCompleteRequest:
        if self.status == "succeeded" and self.outcome == "incomplete":
            raise ValueError("a successful fanout review cannot be incomplete")
        if self.status == "incomplete" and self.outcome != "incomplete":
            raise ValueError("an incomplete fanout review needs incomplete outcome")
        if len(json.dumps(self.report, separators=(",", ":"))) > 512_000:
            raise ValueError("fanout report exceeds 512000 bytes")
        usage = self.report.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("fanout report is missing usage")
        reported = usage.get("reported_cost_usd")
        if reported is not None and (
            not isinstance(reported, (int, float))
            or isinstance(reported, bool)
            or not math.isfinite(reported)
            or reported < 0
        ):
            raise ValueError("fanout reported cost is invalid")
        return self


class FanoutShadowCompleteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    accepted: bool


class AdminFanoutShadowReview(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    shadow_id: UUID
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str
    policy_version: int
    policy_manifest_profile: Literal["core", "l1", "l1_l2"]
    policy_manifest_rotation_id: str
    policy_manifest_digest: str
    settings_revision: int
    settings_scope: str
    settings_checksum: str
    status: FanoutShadowStatus
    outcome: FanoutShadowOutcome | None
    baseline: dict
    report: dict | None
    disagrees_with_baseline: bool | None
    coverage_complete: bool | None
    error_code: str | None
    provider: str | None
    reserved_cost_usd: float
    reported_cost_usd: float | None
    unmetered: bool
    reserved_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AdminFanoutShadowMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int
    queued: int
    running: int
    succeeded: int
    incomplete: int
    skipped: int
    compared: int
    disagreements: int
    incomplete_coverage: int
    rolling_24h_reserved_cost_usd: float
    rolling_24h_reported_cost_usd: float
    rolling_24h_unmetered: int


class AdminFanoutShadowResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    metrics: AdminFanoutShadowMetrics
    items: list[AdminFanoutShadowReview]
    count: int
    returned: int
    limit: int
    offset: int
    has_more: bool

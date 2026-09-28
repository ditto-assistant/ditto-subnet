"""Source-safe, versioned controls for low-information submission attempts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

AttemptKind = Literal[
    "first_submission",
    "infrastructure_retry",
    "packaging_only_repair",
    "small_source_delta",
    "material_new_work",
    "inconclusive",
]


class AttemptControlSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: Literal["off", "shadow", "enforce"] = "shadow"
    small_delta_jaccard: Annotated[float, Field(ge=0.90, le=1.0)] = 0.98
    lineage_jaccard: Annotated[float, Field(ge=0.70, le=1.0)] = 0.90
    low_information_limit: Annotated[int, Field(ge=1, le=100)] = 3
    fast_repair_limit: Annotated[int, Field(ge=1, le=10)] = 2
    window_seconds: Annotated[int, Field(ge=3600, le=604800)] = 86400
    cooldown_seconds: Annotated[int, Field(ge=60, le=21600)] = 3600

    @model_validator(mode="after")
    def ordered_thresholds(self) -> AttemptControlSettings:
        if self.lineage_jaccard > self.small_delta_jaccard:
            raise ValueError("lineage_jaccard must not exceed small_delta_jaccard")
        return self


class AttemptGuidance(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    policy_revision: int
    settings_digest: str = ""
    evaluated_at: datetime | None = None
    mode: Literal["off", "shadow", "enforce"]
    classification: AttemptKind
    reference_agent_id: UUID | None = None
    lineage_agent_id: UUID | None = None
    completed_low_information_attempts: int = 0
    reserved_low_information_attempts: int = 0
    fast_repairs_remaining: int = 0
    fast_repair: bool = False
    appeal_id: UUID | None = None
    retry_at: datetime | None = None
    reason: str


class AttemptSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    settings: AttemptControlSettings
    calibration_id: UUID | None = None
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class ReplayCase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    expected_classification: AttemptKind
    expected_throttled: Annotated[bool, Field(strict=True)]


class AttemptReplayRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    settings: AttemptControlSettings
    cases: Annotated[list[ReplayCase], Field(min_length=1, max_length=500)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"

    @model_validator(mode="after")
    def unique_cases(self) -> AttemptReplayRequest:
        if len({case.agent_id for case in self.cases}) != len(self.cases):
            raise ValueError("replay cases must name distinct submissions")
        return self


class AttemptAppealRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    agent_id: UUID
    expected_policy_revision: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AttemptPolicyRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    revision: int
    parent_revision: int
    settings: AttemptControlSettings
    calibration_id: UUID | None = None
    actor: str
    reason: str
    created_at: datetime | None = None


class AttemptPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    current: AttemptPolicyRevision
    effective_settings: AttemptControlSettings
    enforcement_blocked_reason: str | None = None
    history: list[AttemptPolicyRevision]


class AttemptReplayRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    agent_id: UUID
    guidance: AttemptGuidance
    expected_classification: AttemptKind
    expected_throttled: bool
    would_throttle: bool


class AttemptReplayReport(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    calibration_id: UUID
    settings_digest: str
    case_count: int
    false_throttles: int
    false_allows: int
    classification_mismatches: int
    inconclusive_count: int
    proposed_delays: int
    immediate_admissions_deferred: int
    immediate_admission_deferral_ratio: float
    # Delaying admission does not establish that a benchmark run was saved.
    eligible_for_enforcement: bool
    coverage: dict[str, int]
    rows: list[AttemptReplayRow]


class AttemptAppealResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    appeal_id: UUID
    policy_revision: int
    actor: str
    reason: str
    created_at: datetime


class AttemptRecordResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    guidance: AttemptGuidance
    appeals: list[AttemptAppealResponse]


class AttemptCalibrationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    report: AttemptReplayReport
    actor: str
    reason: str
    created_at: datetime

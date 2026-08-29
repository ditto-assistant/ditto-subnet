"""Private operator models for hosted-inference runtime diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InferenceLaneWindow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    window_seconds: int
    request_kind: Literal["chat", "embedding"]
    calls: int
    calls_per_second: float
    tokens: int
    tokens_per_second: float
    completed: int
    failed: int
    canceled: int
    timed_out: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_max_ms: int | None
    peak_global_concurrency: int


class InferenceLaneCurrent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    request_kind: Literal["chat", "embedding"]
    active_requests: int
    live_grants: int
    stale_started_requests: int
    per_ticket_limit: int
    per_validator_limit: int
    global_limit: int
    per_ticket_rpm_limit: int
    per_validator_rpm_limit: int
    global_rpm_limit: int
    peak_per_ticket_concurrency_60m: int
    peak_per_validator_concurrency_60m: int
    peak_global_concurrency_60m: int


class RelayRuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    target: Literal["platform-relay-1", "platform-relay-2"]
    status: Literal["ok", "unavailable"]
    source_revision: str | None = None
    checked_out_revision: str | None = None
    revision_drift: bool | None = None
    process_started_at: datetime | None = None
    capacity_declines: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class ProviderCircuitSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, from_attributes=True)

    provider: str
    state: Literal["open", "closed"]
    epoch: UUID
    opened_at: datetime
    retry_at: datetime
    last_failure_at: datetime
    closed_at: datetime | None
    failure_count: int
    last_status: int | None
    last_error_code: str
    probe_kind: Literal["scoring", "screening"] | None
    probe_key: str | None
    probe_expires_at: datetime | None


class InferenceRuntimeMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_at: datetime
    settings_revision: int
    settings_checksum: str
    lanes: list[InferenceLaneCurrent]
    windows: list[InferenceLaneWindow]
    relays: list[RelayRuntimeSnapshot]
    provider_circuit: ProviderCircuitSnapshot | None = None


RuntimeProfileTarget = Literal["platform-relay-1", "platform-relay-2"]
RuntimeProfileType = Literal["cpu", "heap", "allocs", "goroutine"]


class RuntimeProfileCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    target: RuntimeProfileTarget
    profile_type: RuntimeProfileType
    seconds: Annotated[int | None, Field(ge=5, le=30)] = None
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str

    @model_validator(mode="after")
    def _duration_matches_profile(self) -> RuntimeProfileCaptureRequest:
        if self.profile_type == "cpu" and self.seconds is None:
            raise ValueError("seconds is required for a CPU profile")
        if self.profile_type != "cpu" and self.seconds is not None:
            raise ValueError("seconds is only valid for a CPU profile")
        return self


class RuntimeProfileArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    profile_id: UUID
    target: RuntimeProfileTarget
    profile_type: RuntimeProfileType
    seconds: int | None
    source_revision: str
    checked_out_revision: str
    revision_drift: bool
    actor: str
    reason: str
    created_at: datetime
    expires_at: datetime
    byte_size: int
    sha256: str
    media_type: str = "application/octet-stream"
    filename: str
    download_path: str

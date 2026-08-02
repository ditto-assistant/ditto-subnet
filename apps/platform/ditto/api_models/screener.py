"""Screener wire shapes layered over the shared verdict protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.system_health import SystemMetrics
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ScreenedImageCompletedPart,
    ScreenedImageUploadRequest,
    ScreenedImageUploadResponse,
    ScreenerQueueItem,
    ScreenerQueueResponse,
    ScreenEvidenceItem,
    ScreenResultRequest,
    ScreenResultResponse,
    ScreenReviewAudit,
    SourceReviewEvidenceItem,
    SourceReviewFinding,
)
from ditto_screening_protocol import (
    ScreenedImagePartUploadRequest as ScreenedImagePartRequest,
)
from ditto_screening_protocol import (
    ScreenedImagePartUploadResponse as ScreenedImagePartResponse,
)
from ditto_screening_protocol import (
    ScreenedImageUploadAbortRequest as ScreenedImageAbortRequest,
)
from ditto_screening_protocol import (
    ScreenedImageUploadAbortResponse as ScreenedImageAbortResponse,
)
from ditto_screening_protocol import (
    ScreenedImageUploadCompleteRequest as ScreenedImageCompleteRequest,
)
from ditto_screening_protocol import (
    ScreenedImageUploadCompleteResponse as ScreenedImageCompleteResponse,
)

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_SOFTWARE_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
# Per-instance identity (heartbeat v3). Excludes ':' so it can never break the
# colon-delimited signing message; mirrors ditto-screener's _INSTANCE_ID_PATTERN.
_INSTANCE_ID_PATTERN = r"^[a-zA-Z0-9._-]{1,63}$"

ScreenerRuntimeState = Literal["polling", "screening", "error", "paused"]
ScreenerProgressStage = Literal[
    "preparing",
    "downloading",
    "validating",
    "building",
    "starting",
    "health_check",
    "source_review_0",
    "source_review_10",
    "source_review_20",
    "source_review_30",
    "source_review_40",
    "source_review_50",
    "source_review_60",
    "source_review_70",
    "source_review_80",
    "source_review_90",
    "source_review_100",
    "submitting",
]


class ScreenerProgress(BaseModel):
    """Signed, public-safe progress for one active screening job."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: ScreenerProgressStage
    started_at: Annotated[int, Field(ge=0)]


class ScreenerReviewSettingsStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    revision: Annotated[int, Field(ge=0)]
    scope: Annotated[str, Field(pattern=r"^(?:bootstrap|\*|[a-zA-Z0-9._-]{1,63})$")]
    mode: Literal["off", "shadow", "enforce"]
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source: Literal["platform", "cache", "bootstrap"]


class ScreenerHeartbeatRequest(BaseModel):
    """Dedicated screener identity, work, and optional host-health report."""

    model_config = ConfigDict(extra="forbid")

    screener_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    software_version: Annotated[str, Field(pattern=_SOFTWARE_VERSION_PATTERN)]
    protocol_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    policy_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    state: ScreenerRuntimeState
    active_agent_id: UUID | None = None
    # Optional so v1/v2 workers stay accepted during rollout; required for v3.
    instance_id: Annotated[str, Field(pattern=_INSTANCE_ID_PATTERN)] | None = None
    progress: ScreenerProgress | None = None
    system_metrics: SystemMetrics | None = None
    review_settings: ScreenerReviewSettingsStatus | None = None
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def validate_instance_id(self) -> ScreenerHeartbeatRequest:
        if self.protocol_version >= 3 and not self.instance_id:
            raise ValueError("heartbeat protocol v3 requires an instance_id")
        return self

    @model_validator(mode="after")
    def validate_progress(self) -> ScreenerHeartbeatRequest:
        if self.progress is None:
            return self
        if self.protocol_version < 2:
            raise ValueError("progress requires heartbeat protocol v2")
        if self.state != "screening" or self.active_agent_id is None:
            raise ValueError("progress requires active screening work")
        if self.progress.started_at > self.timestamp:
            raise ValueError("progress start cannot be after the heartbeat")
        if self.timestamp - self.progress.started_at > 6 * 60 * 60:
            raise ValueError("progress start is outside the bounded job window")
        return self

    @model_validator(mode="after")
    def validate_review_settings(self) -> ScreenerHeartbeatRequest:
        if self.protocol_version >= 4 and self.review_settings is None:
            raise ValueError("heartbeat protocol v4 requires review settings status")
        if self.protocol_version < 4 and self.review_settings is not None:
            raise ValueError("review settings status requires heartbeat protocol v4")
        return self


class ScreenerHeartbeatResponse(BaseModel):
    """Acknowledgement that a signed screener heartbeat was persisted."""

    accepted: bool
    seen_at: datetime


class ShadowReviewUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    reasoning_tokens: Annotated[int, Field(ge=0)]
    estimated_cost_usd: Annotated[float, Field(ge=0, le=25)]
    reported_cost_usd: Annotated[float, Field(ge=0, le=25)] | None = None


# One shadow observation records every provider stage the L2/L3 trajectory
# used: each analyst turn, the critic, and every adjudicator, including model
# failover retries. A bounded escalation therefore reports far more than a
# handful of stages -- observed production runs span 9 to 25. The screener
# caps this at 50 (_MAX_SHADOW_PROVIDER_STAGES in ditto_screener), and this
# bound must not be tighter, or the platform silently rejects the telemetry it
# asked the screener to collect.
MAX_SHADOW_PROVIDER_STAGES = 50


class ShadowReviewObservationRequest(BaseModel):
    """Bounded, non-authoritative observation for an active attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    settings_revision: Annotated[int, Field(ge=1)]
    settings_scope: Annotated[str, Field(pattern=r"^(?:\*|[a-zA-Z0-9._-]{1,63})$")]
    settings_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    disposition: Literal["safe", "violation", "inconclusive", "retryable_infra"]
    risk_level: Literal["low", "medium", "high"] | None = None
    categories: tuple[Annotated[str, Field(max_length=64)], ...] = ()
    finding_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    resolution_basis: Annotated[str, Field(max_length=80)] | None = None
    clearance_path: Annotated[str, Field(max_length=100)] | None = None
    critic_disposition: Annotated[str, Field(max_length=80)] | None = None
    adjudicator_disposition: Annotated[str, Field(max_length=80)] | None = None
    response_models: tuple[Annotated[str, Field(max_length=100)], ...] = ()
    response_providers: tuple[Annotated[str, Field(max_length=100)], ...] = ()
    usage: ShadowReviewUsage

    @model_validator(mode="after")
    def validate_bounds(self) -> ShadowReviewObservationRequest:
        if len(self.categories) > 8:
            raise ValueError("shadow review has too many categories")
        if (
            len(self.response_models) > MAX_SHADOW_PROVIDER_STAGES
            or len(self.response_providers) > MAX_SHADOW_PROVIDER_STAGES
        ):
            raise ValueError("shadow review has too many provider stages")
        if self.disposition in {"safe", "violation"} and self.risk_level is None:
            raise ValueError("decisive shadow review requires a risk level")
        return self


class ShadowReviewObservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    accepted: bool


__all__ = [
    "SCREENING_POLICY_VERSION",
    "ScreenerQueueItem",
    "ScreenerQueueResponse",
    "ScreenerHeartbeatRequest",
    "ScreenerHeartbeatResponse",
    "ShadowReviewObservationRequest",
    "ShadowReviewObservationResponse",
    "ScreenerProgress",
    "ScreenerProgressStage",
    "ScreenerRuntimeState",
    "ScreenEvidenceItem",
    "ScreenedImageUploadRequest",
    "ScreenedImageUploadResponse",
    "ScreenedImagePartRequest",
    "ScreenedImagePartResponse",
    "ScreenedImageCompletedPart",
    "ScreenedImageCompleteRequest",
    "ScreenedImageCompleteResponse",
    "ScreenedImageAbortRequest",
    "ScreenedImageAbortResponse",
    "ScreenResultRequest",
    "ScreenResultResponse",
    "ScreenReviewAudit",
    "SourceReviewEvidenceItem",
    "SourceReviewFinding",
]

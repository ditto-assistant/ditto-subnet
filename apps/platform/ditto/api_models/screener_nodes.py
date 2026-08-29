"""Provider-neutral screener enrollment and capacity-control contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.screener_node_settings import (
    ScreenerNodeChannelSettingsControl,
)
from ditto.api_models.screener_provider_settings import ScreenerProviderSettingsControl
from ditto_screening_protocol import SourceReviewObservationPayload

ScreenerProvider = Literal["gcp", "targon", "hetzner", "home", "test"]
ScreenerNodeStatus = Literal["active", "draining", "quarantined", "revoked"]
TrustedImageBuildStatus = Literal[
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "fallback_required",
    "canceled",
]

_NODE_ID = r"^[a-zA-Z0-9._-]{1,63}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE = r"^[0-9a-fA-F]{128}$"
_EPOCH = r"^[a-zA-Z0-9._:-]{1,100}$"
_IMAGE_REFERENCE = r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"


class ScreenerBootstrapGrantRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider: ScreenerProvider
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    image_reference: Annotated[str, Field(pattern=_IMAGE_REFERENCE)] | None = None


class ScreenerBootstrapGrantAdminRequest(BaseModel):
    """Audited operator request for one controller-fenced enrollment grant."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider: ScreenerProvider
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    image_reference: Annotated[str, Field(pattern=_IMAGE_REFERENCE)]
    expected_controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)]
    confirmation: Annotated[str, Field(min_length=1, max_length=600)]


class ScreenerBootstrapGrantResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    grant_id: UUID
    registration_token: Annotated[str, Field(min_length=43, max_length=128)]
    expires_at: datetime


def screener_bootstrap_grant_confirmation(
    payload: ScreenerBootstrapGrantAdminRequest,
) -> str:
    return (
        f"CREATE SCREENER BOOTSTRAP GRANT NODE={payload.node_id} "
        f"PROVIDER={payload.provider} RESOURCE={payload.provider_resource_id} "
        f"IMAGE={payload.image_reference}"
    )


class ScreenerNodeRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider: ScreenerProvider
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE)]
    registration_id: UUID


class ScreenerNodeCredentialResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    api_token: Annotated[str, Field(min_length=43, max_length=128)]
    expires_at: datetime


class ScreenerNodeRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE)]
    refresh_id: UUID


class ScreenerControllerFenceRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class ScreenerNodeStatusRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    status: ScreenerNodeStatus
    reason: Annotated[str, Field(min_length=8)]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class ScreenerControllerNodeState(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    provider: ScreenerProvider
    status: ScreenerNodeStatus
    ready: bool
    active_lease: bool
    screening_concurrency: Annotated[int, Field(ge=0)] = 0
    image_reference: str | None = None
    heartbeat_seen_at: datetime | None = None


class ScreenerControllerNodesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    nodes: tuple[ScreenerControllerNodeState, ...]


class ScreenerCapacityEventRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
    provider: ScreenerProvider | None = None
    node_id: Annotated[str, Field(pattern=_NODE_ID)] | None = None
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class ScreenerCapacitySnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    controller_source_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    provider_settings_revision: Annotated[int, Field(ge=0)] = 0
    provider_ready: bool
    runnable_backlog: Annotated[int, Field(ge=0)]
    active_leases: Annotated[int, Field(ge=0)]
    desired_slots: Annotated[int, Field(ge=0)]
    global_cap: Annotated[int, Field(ge=0)]
    targon_capability: Literal["go", "nogo", "unknown"]
    targon_available: Annotated[int, Field(ge=0)]
    targon_healthy: Annotated[int, Field(ge=0)]
    targon_pending: Annotated[int, Field(ge=0)]
    targon_draining: Annotated[int, Field(ge=0)]
    gce_target: Annotated[int, Field(ge=0)]
    gce_healthy: Annotated[int, Field(ge=0)]
    gce_pending: Annotated[int, Field(ge=0)]
    gce_draining: Annotated[int, Field(ge=0)]
    fallback_reason: Annotated[str, Field(max_length=160)] | None = None
    last_provider_success_at: datetime | None = None
    last_provider_error_code: (
        Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None
    ) = None
    last_provider_error_at: datetime | None = None
    events: tuple[ScreenerCapacityEventRequest, ...] = ()


class ScreenerCapacitySnapshotResponse(ScreenerCapacitySnapshotRequest):
    model_config = ConfigDict(extra="ignore", frozen=True)

    controller_heartbeat_at: datetime
    controller_lease_expires_at: datetime
    updated_at: datetime


class ScreenerNodeView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: str
    node_id: str
    provider: ScreenerProvider
    provider_resource_id: str
    screener_hotkey: str
    status: ScreenerNodeStatus
    capacity: int
    image_reference: str | None = None
    token_expires_at: datetime
    registered_at: datetime
    rotated_at: datetime
    revoked_at: datetime | None = None
    status_reason: str | None = None
    heartbeat_seen_at: datetime | None = None
    software_version: str | None = None
    protocol_version: int | None = None
    policy_version: int | None = None
    current_phase: str | None = None


class ScreenerCapacityEventView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: UUID
    event_type: str
    provider: ScreenerProvider | None = None
    node_id: str | None = None
    detail: str
    controller_epoch: str
    created_at: datetime


class TrustedImageBuildCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    component: Literal["screener"]
    source_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    reason: Annotated[str, Field(min_length=8)]


class TrustedImageBuildRetryRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    expected_status: Literal["failed", "fallback_required", "canceled"]
    expected_attempt_count: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=8)]


class TrustedImageBuildView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    build_id: UUID
    environment: str
    component: Literal["screener"]
    source_repository: str
    source_sha: str
    context_path: str
    dockerfile_path: str
    destination: str
    status: TrustedImageBuildStatus
    provider: Literal["targon", "gcp", "hetzner"] | None = None
    provider_resource_id: str | None = None
    image_digest: str | None = None
    error_code: str | None = None
    attempt_count: int
    controller_epoch: str | None = None
    lease_expires_at: datetime | None = None
    created_by: str
    reason: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class TrustedImageBuildClaimRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class TrustedImageBuildClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    build: TrustedImageBuildView | None


class TrustedImageBuildUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    status: Literal["running", "succeeded", "failed", "fallback_required"]
    provider: Literal["targon", "gcp", "hetzner"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None


class SubmissionImageBuildClaimView(BaseModel):
    """One miner build leased to an isolated provider executor."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    build_id: UUID
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_ref: Annotated[
        str,
        Field(pattern=r"^ditto-screen/[0-9a-f-]{73}:latest$"),
    ]
    job_token: Annotated[str, Field(min_length=43, max_length=128)]
    job_token_expires_at: datetime


class SubmissionImageBuildClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    build: SubmissionImageBuildClaimView | None


class SubmissionImageBuildControllerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    status: Literal["running", "fallback_required"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_error(self) -> SubmissionImageBuildControllerUpdateRequest:
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("fallback build update requires an error code")
        if self.status == "running" and self.error_code is not None:
            raise ValueError("running build update cannot carry an error code")
        return self


class SubmissionImageBuildCleanupRequest(BaseModel):
    """Durable notice that a suspended provider rental still needs deletion."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]


class SubmissionImageBuildControllerStatusResponse(BaseModel):
    """Authority-free completion state used by the provider controller."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    build_id: UUID
    status: Literal[
        "queued",
        "leased",
        "running",
        "succeeded",
        "fallback_required",
        "canceled",
        "consumed",
    ]


class ScreenerNodeJobClaimRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]


class ScreenerNodeJobUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: Literal["running", "fallback_required"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_error(self) -> ScreenerNodeJobUpdateRequest:
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("fallback update requires an error code")
        if self.status == "running" and self.error_code is not None:
            raise ValueError("running update cannot carry an error code")
        return self


class ScreenerNodeRuntimeResultRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: Literal["running", "succeeded", "fallback_required"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    image_reference: Annotated[str, Field(pattern=_IMAGE_REFERENCE)] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> ScreenerNodeRuntimeResultRequest:
        if self.status == "succeeded" and self.provider_resource_id is None:
            raise ValueError("successful runtime smoke requires provider provenance")
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("runtime fallback requires an error code")
        if self.status == "running" and self.provider_resource_id is None:
            raise ValueError("running runtime smoke requires a provider resource")
        return self


class SubmissionRuntimeArtifactResponse(BaseModel):
    """Verified build archive made available only to the fenced controller."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    build_id: UUID
    archive_url_b64: str
    output_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    output_size_bytes: Annotated[int, Field(gt=0, le=4 * 1024**3)]
    destination: Annotated[
        str,
        Field(pattern=r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+:[a-z0-9._-]+$"),
    ]


class SubmissionRuntimeArtifactClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    artifact: SubmissionRuntimeArtifactResponse | None


class SubmissionRuntimeResultRequest(BaseModel):
    """Terminal direct-image Rental result, fenced to one build/controller."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    status: Literal["running", "succeeded", "fallback_required"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    image_reference: Annotated[str, Field(pattern=_IMAGE_REFERENCE)] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> SubmissionRuntimeResultRequest:
        if self.status == "succeeded" and (
            self.provider_resource_id is None or self.image_reference is None
        ):
            raise ValueError("successful runtime smoke requires provider provenance")
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("runtime fallback requires an error code")
        if self.status == "running" and self.provider_resource_id is None:
            raise ValueError("running runtime smoke requires a provider resource")
        return self


class SubmissionSourceReviewClaimView(BaseModel):
    """One read-only source review leased to a trusted provider worker."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    review_id: UUID
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_reference: Annotated[str, Field(pattern=_IMAGE_REFERENCE)]
    job_token: Annotated[str, Field(min_length=43, max_length=128)]
    job_token_expires_at: datetime


class SubmissionSourceReviewClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    review: SubmissionSourceReviewClaimView | None


class SubmissionSourceReviewControllerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    status: Literal["running", "fallback_required"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def validate_error(self) -> SubmissionSourceReviewControllerUpdateRequest:
        if self.status == "fallback_required" and self.error_code is None:
            raise ValueError("fallback source-review update requires an error code")
        if self.status == "running" and self.error_code is not None:
            raise ValueError("running source-review update cannot carry an error code")
        return self


class SubmissionSourceReviewControllerStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    review_id: UUID
    status: Literal[
        "queued",
        "leased",
        "running",
        "succeeded",
        "fallback_required",
        "canceled",
        "consumed",
    ]


class SubmissionSourceReviewCleanupRequest(BaseModel):
    """Durable notice that a provider Rental still needs deletion."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]


class SubmissionSourceReviewSourceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_url_b64: str
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SubmissionSourceReviewCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    observation: SourceReviewObservationPayload


class SubmissionSourceReviewCompleteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    verified: Literal[True]


class SubmissionBuildSourceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_url_b64: str
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_ref: Annotated[
        str,
        Field(pattern=r"^ditto-screen/[0-9a-f-]{73}:latest$"),
    ]


class SubmissionBuildUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    output_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    output_size_bytes: Annotated[int, Field(gt=0, le=4 * 1024**3)]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class SubmissionBuildUploadResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    upload_url_b64: str
    required_headers: dict[str, str]
    expires_at: datetime


class SubmissionBuildCompleteRequest(SubmissionBuildUploadRequest):
    pass


class SubmissionBuildCompleteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    verified: Literal[True]


class ScreenerProviderJobView(BaseModel):
    """Redacted recent one-shot provider work for operator visibility."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    job_id: UUID
    lane: Literal["build", "runtime", "source_review"]
    status: str
    provider: Literal["targon", "gcp", "hetzner"] | None = None
    node_id: str | None = None
    provider_resource_id: str | None = None
    image_reference: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class ScreenerCapacityView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    snapshot: ScreenerCapacitySnapshotResponse | None
    nodes: list[ScreenerNodeView]
    events: list[ScreenerCapacityEventView]
    builds: list[TrustedImageBuildView] = Field(default_factory=list)
    provider_jobs: list[ScreenerProviderJobView] = Field(default_factory=list)
    provider_control: ScreenerProviderSettingsControl
    node_controls: list[ScreenerNodeChannelSettingsControl] = Field(
        default_factory=list
    )

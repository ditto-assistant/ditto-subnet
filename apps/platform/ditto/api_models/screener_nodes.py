"""Provider-neutral screener enrollment and capacity-control contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class ScreenerBootstrapGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider: ScreenerProvider
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class ScreenerBootstrapGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    registration_token: Annotated[str, Field(min_length=43, max_length=128)]
    expires_at: datetime


class ScreenerNodeRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider: ScreenerProvider
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE)]
    registration_id: UUID


class ScreenerNodeCredentialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    api_token: Annotated[str, Field(min_length=43, max_length=128)]
    expires_at: datetime


class ScreenerNodeRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    screener_hotkey: Annotated[str, Field(pattern=_SS58)]
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE)]
    refresh_id: UUID


class ScreenerControllerFenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class ScreenerNodeStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    status: ScreenerNodeStatus
    reason: Annotated[str, Field(min_length=8)]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class ScreenerControllerNodeState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: Annotated[str, Field(pattern=_NODE_ID)]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)]
    provider: ScreenerProvider
    status: ScreenerNodeStatus
    ready: bool
    active_lease: bool
    heartbeat_seen_at: datetime | None = None


class ScreenerControllerNodesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[ScreenerControllerNodeState, ...]


class ScreenerCapacityEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
    provider: ScreenerProvider | None = None
    node_id: Annotated[str, Field(pattern=_NODE_ID)] | None = None
    detail: Annotated[str, Field(min_length=1, max_length=500)]


class ScreenerCapacitySnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    runnable_backlog: Annotated[int, Field(ge=0)]
    active_leases: Annotated[int, Field(ge=0)]
    desired_slots: Annotated[int, Field(ge=0)]
    global_cap: Annotated[int, Field(ge=0)]
    provider_ready: bool
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
    model_config = ConfigDict(extra="forbid", frozen=True)

    controller_heartbeat_at: datetime
    controller_lease_expires_at: datetime
    updated_at: datetime


class ScreenerNodeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str
    node_id: str
    provider: ScreenerProvider
    provider_resource_id: str
    screener_hotkey: str
    status: ScreenerNodeStatus
    capacity: int
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
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    event_type: str
    provider: ScreenerProvider | None = None
    node_id: str | None = None
    detail: str
    controller_epoch: str
    created_at: datetime


class TrustedImageBuildCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["screener"]
    source_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    reason: Annotated[str, Field(min_length=8)]


class TrustedImageBuildView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build_id: UUID
    environment: str
    component: Literal["screener"]
    source_repository: str
    source_sha: str
    context_path: str
    dockerfile_path: str
    destination: str
    status: TrustedImageBuildStatus
    provider: Literal["targon", "gcp"] | None = None
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
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]


class TrustedImageBuildClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build: TrustedImageBuildView | None


class TrustedImageBuildUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    controller_epoch: Annotated[str, Field(pattern=_EPOCH)]
    status: Literal["running", "succeeded", "failed", "fallback_required"]
    provider: Literal["targon", "gcp"]
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=200)] | None = (
        None
    )
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")] | None = None


class ScreenerCapacityView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: ScreenerCapacitySnapshotResponse | None
    nodes: list[ScreenerNodeView]
    events: list[ScreenerCapacityEventView]
    builds: list[TrustedImageBuildView] = Field(default_factory=list)

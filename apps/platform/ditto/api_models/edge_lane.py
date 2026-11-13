"""Operator settings for the Ditto Edge shadow lane (SN-481).

Ditto Edge pays opt-in Mac nodes, in shadow only, for holding public,
content-addressed Ditto objects that Platform assigns and challenges. These
settings are the whole lane policy, stored one append-only revision at a time
like every other operator control here.

The lane is ``off`` by default. Deploying it changes nothing. There is no
``enforce`` mode: in ``shadow`` every ledger row is stored
``weight_eligible = false`` behind a database constraint, so leaving shadow
takes a new contract revision and a migration, not a settings write. See
``docs/design/sn481-miner-hostable-workloads.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_edge_protocol.limits import GIB, MAX_OBJECT_BYTES_CEILING, MIB
from ditto_edge_protocol.settings import LaneSettings

EdgeLaneMode = Literal["off", "shadow"]

CONFIRMATION = "APPLY EDGE LANE SETTINGS"
"""The exact phrase every settings write must carry."""

MAX_TOTAL_OBJECT_BYTES = 16 * GIB
"""Ceiling on the operator's total-ingest cap. The shadow lane holds public
objects an operator ingests deliberately; it is not a storage service."""


class EdgeLaneSettings(BaseModel):
    """The complete lane policy (one revision = the whole object).

    Mirrors :class:`ditto_edge_protocol.settings.LaneSettings` field for field,
    and validates by building one, so Platform can never accept a policy the
    shared lane rules would refuse. ``max_total_object_bytes`` is
    Platform-only: it bounds what operators may ingest into object storage.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: EdgeLaneMode = "off"
    max_nodes: Annotated[int, Field(ge=1, le=10_000)] = 100
    replicas: Annotated[int, Field(ge=1, le=16)] = 3
    max_object_bytes: Annotated[int, Field(ge=1, le=MAX_OBJECT_BYTES_CEILING)] = 8 * MIB
    max_total_object_bytes: Annotated[int, Field(ge=0, le=MAX_TOTAL_OBJECT_BYTES)] = GIB
    node_quota_cap_bytes: Annotated[int, Field(ge=0)] = 2 * GIB
    lease_seconds: Annotated[int, Field(ge=60, le=86_400)] = 900
    poll_interval_seconds: Annotated[int, Field(ge=5, le=3600)] = 30
    challenge_deadline_seconds: Annotated[int, Field(ge=5, le=3600)] = 60
    window_seconds: Annotated[int, Field(ge=300, le=86_400)] = 3600
    challenges_per_window: Annotated[int, Field(ge=0, le=120)] = 6
    retrieval_share: Annotated[float, Field(ge=0.0, le=1.0)] = 0.2
    revoked_share: Annotated[float, Field(ge=0.0, le=1.0)] = 0.15
    min_challenges: Annotated[int, Field(ge=0, le=120)] = 3
    availability_floor: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8
    max_new_assignments_per_poll: Annotated[int, Field(ge=0, le=256)] = 16
    revoked_lookback_seconds: Annotated[int, Field(ge=0, le=30 * 86_400)] = 86_400

    @model_validator(mode="after")
    def _coherent(self) -> EdgeLaneSettings:
        # The shared rules own the cross-field checks (shares sum, leases
        # covering two polls, the minimum inside the slot count).
        self.lane()
        return self

    def lane(self) -> LaneSettings:
        """The same policy as the shared lane rules take it."""
        return LaneSettings(
            mode=self.mode,
            max_nodes=self.max_nodes,
            replicas=self.replicas,
            max_object_bytes=self.max_object_bytes,
            node_quota_cap_bytes=self.node_quota_cap_bytes,
            lease_seconds=self.lease_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            challenge_deadline_seconds=self.challenge_deadline_seconds,
            window_seconds=self.window_seconds,
            challenges_per_window=self.challenges_per_window,
            retrieval_share=self.retrieval_share,
            revoked_share=self.revoked_share,
            min_challenges=self.min_challenges,
            availability_floor=self.availability_floor,
            max_new_assignments_per_poll=self.max_new_assignments_per_poll,
            revoked_lookback_seconds=self.revoked_lookback_seconds,
        )


class EdgeLaneSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: EdgeLaneSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveEdgeLaneSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: EdgeLaneSettings
    checksum: str
    source: Literal["revision", "default"]
    max_age_seconds: float
    """Resolver TTL: the longest a node request can still see the previous
    revision."""
    weight_eligible: Literal[False] = False
    """Always false. Nothing in this lane reaches weights, emissions or track
    shares while it is shadow."""


class AdminEdgeLaneSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: EdgeLaneSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminEdgeLaneSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[EdgeLaneSettingsRevision]
    history: list[EdgeLaneSettingsRevision]
    default: EdgeLaneSettings
    effective: EffectiveEdgeLaneSettings

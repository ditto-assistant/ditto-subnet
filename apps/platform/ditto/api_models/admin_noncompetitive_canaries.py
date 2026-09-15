"""Audited operator control for noncompetitive team canary exclusions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.upload import _SS58_PATTERN

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Hotkey = Annotated[str, Field(pattern=_SS58_PATTERN)]


class AdminTeamCanaryReserveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    miner_hotkey: Hotkey
    artifact_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str


class AdminTeamCanaryBindRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    agent_id: UUID
    miner_hotkey: Hotkey
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str


class AdminTeamCanaryAgent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    status: AgentStatus
    sha256: str
    screened_image_sha256: str | None
    state: Literal["reserved", "bound", "binding_drift"]
    competition_excluded: Literal[True] = True


class AdminTeamCanaryExclusion(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    exclusion_id: UUID
    kind: Literal["team_canary"] = "team_canary"
    miner_hotkey: str
    artifact_sha256: str
    reason: str
    created_by: str
    created_at: datetime
    agent_id: UUID | None
    screened_image_sha256: str | None
    bound_by: str | None
    bound_reason: str | None
    bound_at: datetime | None
    matched_agents: list[AdminTeamCanaryAgent]


class AdminTeamCanaryList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int
    exclusions: list[AdminTeamCanaryExclusion]

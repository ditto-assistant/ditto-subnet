"""Wire models for miner device-code login, sessions, and public profiles."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.name_claim import NameClaimProof, PublicNameHandle

MinerScope = Literal["read", "profile", "download", "upload", "handle", "challenges"]


class MinerDeviceStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scopes: list[MinerScope] = Field(min_length=1, max_length=6)
    ttl_seconds: int = Field(ge=3600, le=2_592_000)
    client_id: str | None = Field(default=None, max_length=80)
    redirect_uri: str | None = Field(default=None, max_length=500)
    state: str | None = Field(default=None, max_length=256)
    code_challenge: str | None = Field(default=None, max_length=128)


class MinerDeviceStartResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_code: str
    poll_token: str | None = None
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int = 2
    scopes: list[MinerScope]
    ttl_seconds: int
    login_command: str


class MinerDevicePublicResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_code: str
    grant_id: UUID
    status: Literal["pending", "approved", "expired", "denied", "consumed"]
    scopes: list[MinerScope]
    ttl_seconds: int
    expires_in: int
    login_command: str
    miner_hotkey: str | None = None
    oauth: bool = False


class MinerLoginApproveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    netuid: int = Field(ge=1)
    miner_hotkey: str = Field(min_length=47, max_length=48)
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class MinerSessionView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: UUID
    miner_hotkey: str
    scopes: list[MinerScope]
    label: Literal["dashboard", "mcp", "cli"]
    created_at: datetime
    expires_at: datetime
    expires_in: int


class MinerDeviceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_code: str
    status: Literal["pending", "approved", "expired", "denied", "consumed"]
    scopes: list[MinerScope]
    ttl_seconds: int
    session: MinerSessionView | None = None
    access_token: str | None = None
    token_type: str | None = None
    continue_url: str | None = None


class MinerProfileLinks(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x_url: str | None = None
    github_url: str | None = None
    discord_handle: str | None = None


class MinerProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    x_url: str | None = Field(default=None, max_length=200)
    github_url: str | None = Field(default=None, max_length=200)
    discord_handle: str | None = Field(default=None, max_length=32)


class MinerCommand(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    command: str
    reason: str
    submit_url: str | None = None


class PublicMinerSubmission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    name: str
    status: str
    created_at: datetime
    avatar_url: str | None = None


class PublicMinerReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["ath", "dispute"]
    agent_id: UUID
    name: str
    status: str
    opened_at: datetime
    detail: str | None = None


class PublicMinerProfileResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    miner_hotkey: str
    name_handle: PublicNameHandle | None = None
    avatar_url: str | None = None
    profile: MinerProfileLinks
    profile_url: str
    submissions: list[PublicMinerSubmission]
    generated_at: datetime


class MinerMeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session: MinerSessionView
    profile: MinerProfileLinks
    name_handle: PublicNameHandle | None = None
    avatar_url: str | None = None
    profile_url: str
    commands: list[MinerCommand]


class MinerMeReviewsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    generated_at: datetime
    reviews: list[PublicMinerReview]

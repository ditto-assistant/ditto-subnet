"""Wire models for miner device-code login. Mirror of Platform models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.name_claim import NameClaimProof

MinerScope = Literal["read", "profile", "download", "upload", "handle", "challenges"]


class MinerDeviceStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scopes: list[MinerScope] = Field(min_length=1, max_length=6)
    ttl_seconds: int = Field(ge=3600, le=2_592_000)


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
    status: str
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
    label: str
    created_at: datetime
    expires_at: datetime
    expires_in: int


class MinerDeviceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_code: str
    status: str
    scopes: list[MinerScope]
    ttl_seconds: int
    session: MinerSessionView | None = None
    access_token: str | None = None
    token_type: str | None = None
    continue_url: str | None = None

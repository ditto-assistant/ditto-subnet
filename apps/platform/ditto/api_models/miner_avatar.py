"""Wire models for signed miner profile-picture updates."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.name_claim import NameClaimProof

KeyKind = Literal["hotkey", "coldkey"]


class MinerAvatarSetRequest(BaseModel):
    """JSON fields posted alongside the avatar file."""

    model_config = ConfigDict(extra="ignore")

    netuid: int = Field(ge=1)
    miner_hotkey: str = Field(min_length=47, max_length=48)
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class MinerAvatarClearRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    netuid: int = Field(ge=1)
    miner_hotkey: str = Field(min_length=47, max_length=48)
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class MinerAvatarResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    miner_hotkey: str
    avatar_url: str | None
    content_type: str | None = None
    sha256: str | None = None
    updated_at: datetime | None = None

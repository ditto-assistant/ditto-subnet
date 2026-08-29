"""Miner-private screening failure feedback wire models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.upload import _SS58_PATTERN


class MinerScreeningFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    status: str
    policy_version: Annotated[int, Field(ge=1)]
    started_at: datetime
    finished_at: datetime | None = None
    reason_code: str | None = None
    public_reason: str | None = None
    provider: str | None = None
    lane: str | None = None
    detail: str | None = None
    log_tail: str | None = None
    captured_at: datetime | None = None


class MinerScreeningFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerScreeningFailure]

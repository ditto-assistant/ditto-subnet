"""Audited operator settings for miner submission admission."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SubmissionSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    cooldown_seconds: int
    fee_amount_rao: int
    reason: str
    actor: str
    created_at: datetime | None


class AdminSubmissionSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: SubmissionSettingsRevision
    history: list[SubmissionSettingsRevision]


class AdminSubmissionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    cooldown_seconds: Annotated[int, Field(ge=60, le=86400)]
    fee_amount_rao: Annotated[int, Field(ge=1, le=1_000_000_000_000)] | None = None
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

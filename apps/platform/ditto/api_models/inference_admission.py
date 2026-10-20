"""Operator view of sanitized inference admission rejections."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InferenceAdmissionRejectionRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    rejection_id: UUID
    created_at: datetime
    lane: str
    http_status: int
    admission_code: str
    grant_id: UUID | None
    validator_hotkey: str | None
    correlation_id: UUID
    request_bytes: int
    byte_limit: int | None
    platform_revision: str


class InferenceAdmissionRejectionSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    grant_id: UUID
    counts: dict[str, Annotated[int, Field(ge=0)]]
    rows: list[InferenceAdmissionRejectionRow]

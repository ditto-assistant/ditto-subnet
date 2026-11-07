"""Miner-side copy of the self-serve screening-retry wire models (SN-456).

Structural twin of ``apps/platform/ditto/api_models/miner_screening_retry.py``
(miner-facing models only; the operator settings models stay Platform-side).
Kept as a separate copy rather than a shared import because the miner CLI ships
independently of the platform and must not depend on it -- the same arrangement
every other miner wire model here already has.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MinerScreeningRetryStatus(BaseModel):
    """Whether the signed-in owner may retry this agent's screening now."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    agent_status: str
    artifact_sha256: str
    latest_attempt_id: UUID | None
    latest_attempt_status: str | None
    reason_code: str | None
    public_reason: str | None
    eligible: bool
    blocker: str | None
    """A bounded code; unknown future values are kept as text."""
    already_authorized: bool
    retries_used: int
    retries_allowed: int
    cost_notice: str


class MinerScreeningRetryRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_attempt_id: UUID
    expected_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    acknowledge_cost: Literal[True]


class MinerScreeningRetryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    override_id: UUID
    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    created_at: datetime
    idempotent: bool
    authorized_by_operator: bool
    retries_used: int
    retries_allowed: int

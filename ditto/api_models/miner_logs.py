"""Miner-side copy of the self-serve harness-diagnostics wire models.

Structural twin of ``apps/platform/ditto/api_models/miner_logs.py``. Kept as a
separate copy rather than a shared import because the miner CLI ships
independently of the platform and must not depend on it -- the same arrangement
every other miner wire model here already has.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from ditto.api_models.upload import _SIGNATURE_HEX_PATTERN, _SS58_PATTERN


class MinerHarnessLogsRequest(BaseModel):
    """Signed proof that the caller holds the hotkey owning ``agent_id``."""

    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]


class MinerHarnessLogAttempt(BaseModel):
    """One validator's attempt at this agent, with whatever it reported."""

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    status: str
    issued_at: datetime
    deadline: datetime
    failed_at: datetime | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    container_log_tail: str | None = None
    """This harness's own bounded, redacted stdout/stderr tail, if any."""


class MinerHarnessLogsResponse(BaseModel):
    """Every recorded attempt at one agent, newest first."""

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerHarnessLogAttempt]

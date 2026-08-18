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

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.upload import _SS58_PATTERN


class MinerHarnessLogAttempt(BaseModel):
    """One validator ticket for this agent, with whatever it last reported."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    status: str
    attempt_count: Annotated[int, Field(ge=1)]
    issued_at: datetime
    deadline: datetime
    failed_at: datetime | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    container_log_tail: str | None = None
    log_tail_attempt: int | None = None
    stale: bool = False


class MinerHarnessLogsResponse(BaseModel):
    """Every recorded validator ticket for one agent, newest first."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerHarnessLogAttempt]

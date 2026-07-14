"""Screener wire shapes layered over the shared verdict protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.system_health import SystemMetrics
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ScreenerQueueItem,
    ScreenerQueueResponse,
    ScreenResultRequest,
    ScreenResultResponse,
)

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_SOFTWARE_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"

ScreenerRuntimeState = Literal["polling", "screening", "error", "paused"]


class ScreenerHeartbeatRequest(BaseModel):
    """Dedicated screener identity, work, and optional host-health report."""

    model_config = ConfigDict(extra="forbid")

    screener_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    software_version: Annotated[str, Field(pattern=_SOFTWARE_VERSION_PATTERN)]
    protocol_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    policy_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    state: ScreenerRuntimeState
    active_agent_id: UUID | None = None
    system_metrics: SystemMetrics | None = None
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]


class ScreenerHeartbeatResponse(BaseModel):
    """Acknowledgement that a signed screener heartbeat was persisted."""

    accepted: bool
    seen_at: datetime


__all__ = [
    "SCREENING_POLICY_VERSION",
    "ScreenerQueueItem",
    "ScreenerQueueResponse",
    "ScreenerHeartbeatRequest",
    "ScreenerHeartbeatResponse",
    "ScreenerRuntimeState",
    "ScreenResultRequest",
    "ScreenResultResponse",
]

"""Operator read model for the dark decision-model lane (Backroom MCP)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ditto.api_models.inference_concurrency_settings import DecisionLaneSettings

DECISION_UPSTREAM = "https://openrouter.ai/api/v1/systemone"
"""The relay's pinned System One endpoint, authenticated with the existing
OpenRouter key (``DITTO_DECISION_UPSTREAM_URL`` may only restate it)."""


class DecisionLaneUsageWindow(BaseModel):
    """Decision-lane ledger totals over one trailing window.

    ``input_tokens`` and ``ledger_cost_microusd`` are receipted usage priced at
    each grant's stamped list price, the same figures the per-run walls and the
    dollar ceiling read. Infra failures (``model_mismatches``,
    ``provider_failures``) are settled uncharged.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    window_seconds: int
    requests: int
    completed: int
    failed: int
    canceled: int
    input_tokens: int
    ledger_cost_microusd: int
    model_mismatches: int
    provider_rejections: int
    provider_failures: int
    grants_with_lane: int
    """Grants with a decision pin whose lease ends inside the window or later."""


class DecisionLaneErrorCount(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    terminal_error_code: str
    requests: int


class DecisionLaneStatus(BaseModel):
    """What the operator board says, and what the lane has actually done."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    state: Literal["disabled", "enabled"]
    """``enabled`` only when the board pins a model and the switch is on."""
    settings_revision: int
    settings_source: Literal["revision", "default"]
    settings: DecisionLaneSettings
    upstream: str
    live_grants_with_lane: int
    """Unexpired grants carrying a decision pin (minted while the lane was on)."""
    usage: list[DecisionLaneUsageWindow]
    terminal_error_codes_24h: list[DecisionLaneErrorCount]
    last_request_at: datetime | None

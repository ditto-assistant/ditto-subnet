"""Admin read: screening review cost by lineage scope."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ScreeningReviewCostGroup(BaseModel):
    """One (lineage scope, terminal status) cell of the window."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    lineage_scope: Literal["diff", "full", "none"]
    status: str
    attempts: int
    attempts_with_telemetry: int
    mean_input_tokens: float | None
    p50_input_tokens: float | None
    p90_input_tokens: float | None
    mean_output_tokens: float | None
    mean_cost_usd: float | None
    total_cost_usd: float | None
    budget_exhausted: int
    mean_wall_minutes: float | None


class ScreeningReviewBudgetReason(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    reason_code: str
    lineage_scope: Literal["diff", "full", "none"]
    attempts: int


class ScreeningReviewCostResponse(BaseModel):
    """Review spend per screening attempt over a lookback window.

    ``attempts`` counts every non-build-only attempt started in the window;
    token and cost figures cover only the attempts whose screener reported
    review usage telemetry (``attempts_with_telemetry``). ``none`` is an
    attempt with no bound lineage packet (an ordinary full review).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    lookback_days: int
    window_start: datetime
    read_at: datetime
    groups: list[ScreeningReviewCostGroup]
    budget_reasons: list[ScreeningReviewBudgetReason]

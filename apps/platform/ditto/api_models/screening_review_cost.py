"""Admin read: screening review cost by lineage scope."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

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


class ScreeningReviewBudgetStage(BaseModel):
    """One (stage, model) row of an attempt's review usage."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    stage: str
    model: str
    requests: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    estimated_cost_usd: float
    reported_cost_usd: float | None
    wall_ms: int | None
    tripped: list[str]


class ScreeningReviewBudgetLimits(BaseModel):
    """Budgets from the review settings revision pinned on the attempt."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    settings_revision: int
    settings_scope: str
    max_input_tokens: int
    max_raw_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float
    l1_max_steps: int
    l1_timeout_seconds: int
    l2_max_steps: int
    l2_timeout_seconds: int
    adjudicator_max_steps: int
    adjudicator_timeout_seconds: int


class ScreeningReviewL2Aggregate(BaseModel):
    """L2/L3 usage summed the way the worker's aggregate budget sums it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    raw_input_tokens: int
    cached_input_tokens: int
    effective_input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    reported_cost_usd: float | None
    billable_cost_usd: float


class ScreeningReviewBudgetStopView(BaseModel):
    """The worker-reported budget stop, verbatim."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    stage: str
    code: str
    tripped: list[str]
    max_raw_input_tokens: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    max_steps: int | None = None
    raw_input_tokens: int = 0
    effective_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    steps_used: int = 0


class ScreeningReviewBudgetLineage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    parent_agent_id: UUID | None
    parent_name: str | None
    parent_version: int | None
    scope: Literal["diff", "full"] | None
    full_review_reasons: list[str] | None
    reference_kind: str | None
    packet_sha256: str | None
    changed_file_count: int | None
    changed_line_count: int | None


class ScreeningReviewBudgetResponse(BaseModel):
    """Per-attempt review model usage, budgets and lineage scope.

    ``telemetry_recorded`` is false when the screener posted no usage (an
    older worker, a build-only lane, no model spend, or a post that failed);
    then every usage field is empty or null, never zero. ``budget_stop`` is
    what the worker reported; ``exceeded_limits`` is re-derived here from the
    L2 aggregate and the pinned limits, so it also covers attempts recorded
    before workers reported a stop.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    attempt_id: UUID
    agent_id: UUID
    status: str
    reason_code: str | None
    build_only: bool
    started_at: datetime
    finished_at: datetime | None
    wall_seconds: float | None
    telemetry_recorded: bool
    recorded_at: datetime | None
    review_elapsed_ms: int | None
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_cost_usd: float | None
    total_estimated_cost_usd: float | None
    stages: list[ScreeningReviewBudgetStage]
    l2_aggregate: ScreeningReviewL2Aggregate | None
    limits: ScreeningReviewBudgetLimits | None
    exceeded_limits: list[str]
    budget_stop: ScreeningReviewBudgetStopView | None
    lineage: ScreeningReviewBudgetLineage

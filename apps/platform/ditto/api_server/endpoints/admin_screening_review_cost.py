"""Admin read of screening review spend per lineage scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screening_review_cost import (
    ScreeningReviewBudgetReason,
    ScreeningReviewCostGroup,
    ScreeningReviewCostResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Any model budget a review can exhaust: the L1 step/read/lease budgets, the
# L2 token/cost budgets and the L3 aggregate (``*-model-total-budget``).
_BUDGET_REASON = "%budget%"

_GROUPS = text(
    """
    SELECT
        COALESCE(a.lineage_scope, 'none') AS lineage_scope,
        a.status AS status,
        count(*) AS attempts,
        count(u.attempt_id) AS attempts_with_telemetry,
        avg(u.total_input_tokens) AS mean_input_tokens,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY u.total_input_tokens)
            AS p50_input_tokens,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY u.total_input_tokens)
            AS p90_input_tokens,
        avg(u.total_output_tokens) AS mean_output_tokens,
        avg(u.total_cost_usd) AS mean_cost_usd,
        sum(u.total_cost_usd) AS total_cost_usd,
        count(*) FILTER (WHERE a.reason_code LIKE :budget) AS budget_exhausted,
        avg(EXTRACT(EPOCH FROM (a.finished_at - a.started_at)) / 60.0)
            FILTER (WHERE a.finished_at IS NOT NULL) AS mean_wall_minutes
    FROM screening_attempts a
    LEFT JOIN screening_review_usage u ON u.attempt_id = a.attempt_id
    WHERE a.started_at >= :window_start AND NOT a.build_only
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
)
_BUDGET_REASONS = text(
    """
    SELECT a.reason_code, COALESCE(a.lineage_scope, 'none') AS lineage_scope,
           count(*) AS attempts
    FROM screening_attempts a
    WHERE a.started_at >= :window_start AND NOT a.build_only
      AND a.reason_code LIKE :budget
    GROUP BY 1, 2
    ORDER BY 3 DESC, 1, 2
    """
)


def _float(value: Any) -> float | None:
    return None if value is None else round(float(value), 4)


@router.get("/screening-review-cost", response_model=ScreeningReviewCostResponse)
async def get_screening_review_cost(
    _admin: AdminDep,
    session: SessionDep,
    lookback_days: Annotated[int, Query(ge=1, le=30)] = 7,
) -> ScreeningReviewCostResponse:
    """Review spend per attempt grouped by lineage scope and terminal status."""
    read_at = datetime.now(UTC)
    window_start = read_at - timedelta(days=lookback_days)
    params = {"window_start": window_start, "budget": _BUDGET_REASON}
    groups = (await session.execute(_GROUPS, params)).mappings().all()
    reasons = (await session.execute(_BUDGET_REASONS, params)).mappings().all()
    return ScreeningReviewCostResponse(
        lookback_days=lookback_days,
        window_start=window_start,
        read_at=read_at,
        groups=[
            ScreeningReviewCostGroup(
                lineage_scope=row["lineage_scope"],
                status=row["status"],
                attempts=int(row["attempts"]),
                attempts_with_telemetry=int(row["attempts_with_telemetry"]),
                mean_input_tokens=_float(row["mean_input_tokens"]),
                p50_input_tokens=_float(row["p50_input_tokens"]),
                p90_input_tokens=_float(row["p90_input_tokens"]),
                mean_output_tokens=_float(row["mean_output_tokens"]),
                mean_cost_usd=_float(row["mean_cost_usd"]),
                total_cost_usd=_float(row["total_cost_usd"]),
                budget_exhausted=int(row["budget_exhausted"]),
                mean_wall_minutes=_float(row["mean_wall_minutes"]),
            )
            for row in groups
        ],
        budget_reasons=[
            ScreeningReviewBudgetReason(
                reason_code=row["reason_code"],
                lineage_scope=row["lineage_scope"],
                attempts=int(row["attempts"]),
            )
            for row in reasons
        ],
    )

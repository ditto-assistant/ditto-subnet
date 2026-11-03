"""Admin reads of screening review spend: per lineage scope and per attempt."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_models.screening_review_cost import (
    ScreeningReviewBudgetLimits,
    ScreeningReviewBudgetLineage,
    ScreeningReviewBudgetReason,
    ScreeningReviewBudgetResponse,
    ScreeningReviewBudgetStage,
    ScreeningReviewBudgetStopView,
    ScreeningReviewCostGroup,
    ScreeningReviewCostResponse,
    ScreeningReviewL2Aggregate,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningReviewUsage,
)

logger = logging.getLogger(__name__)

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


# Mirrors the worker's ``_MAX_AGGREGATE_RAW_INPUT_TOKENS`` (l2_review.py): the
# uncached wire ceiling on L2/L3 input, independent of the settings budget.
MAX_AGGREGATE_RAW_INPUT_TOKENS = 50_000_000


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _cost(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 6)


def _pinned_settings(
    attempt: ScreeningAttempt, revision: ScreenerReviewSettingsRevision | None
) -> ScreenerReviewSettings | None:
    if (
        revision is None
        or revision.checksum != attempt.review_settings_checksum
        or revision.scope != attempt.review_settings_scope
    ):
        return None
    try:
        return ScreenerReviewSettings.model_validate(revision.settings)
    except ValidationError:
        logger.warning(
            "invalid pinned screener settings attempt_id=%s", attempt.attempt_id
        )
        return None


def _l2_aggregate(stages: list[dict[str, Any]]) -> ScreeningReviewL2Aggregate | None:
    """Sum every L2/L3 stage exactly as the worker's aggregate budget does."""
    l2 = [row for row in stages if row.get("stage") != "l1"]
    if not l2:
        return None
    raw = sum(_int(row.get("input_tokens")) for row in l2)
    cached = min(raw, sum(_int(row.get("cached_input_tokens")) for row in l2))
    estimated = sum(_cost(row.get("estimated_cost_usd")) or 0.0 for row in l2)
    reported_each = [_cost(row.get("reported_cost_usd")) for row in l2]
    reported = (
        None
        if any(value is None for value in reported_each)
        else sum(value or 0.0 for value in reported_each)
    )
    return ScreeningReviewL2Aggregate(
        raw_input_tokens=raw,
        cached_input_tokens=cached,
        effective_input_tokens=raw - cached + round(cached * 0.1),
        output_tokens=sum(_int(row.get("output_tokens")) for row in l2),
        estimated_cost_usd=round(estimated, 6),
        reported_cost_usd=None if reported is None else round(reported, 6),
        billable_cost_usd=round(reported if reported is not None else estimated, 6),
    )


def _exceeded(
    aggregate: ScreeningReviewL2Aggregate | None,
    limits: ScreeningReviewBudgetLimits | None,
) -> list[str]:
    if aggregate is None or limits is None:
        return []
    checks = (
        ("raw_input", aggregate.raw_input_tokens > limits.max_raw_input_tokens),
        ("input", aggregate.effective_input_tokens > limits.max_input_tokens),
        ("output", aggregate.output_tokens > limits.max_output_tokens),
        ("cost", aggregate.billable_cost_usd > limits.max_cost_usd),
    )
    return [name for name, over in checks if over]


@router.get(
    "/screening-attempts/{attempt_id}/review-budget",
    response_model=ScreeningReviewBudgetResponse,
    responses={404: {"description": "No attempt with this id."}},
)
async def get_screening_review_budget(
    attempt_id: UUID,
    _admin: AdminDep,
    session: SessionDep,
) -> ScreeningReviewBudgetResponse:
    """One attempt's review model usage per stage, budgets and lineage scope."""
    attempt = await session.get(ScreeningAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="screening attempt not found")
    usage = await session.get(ScreeningReviewUsage, attempt_id)
    revision = (
        await session.get(
            ScreenerReviewSettingsRevision, attempt.review_settings_revision
        )
        if attempt.review_settings_revision is not None
        else None
    )
    parent = (
        await session.get(Agent, attempt.lineage_parent_agent_id)
        if attempt.lineage_parent_agent_id is not None
        else None
    )

    settings = _pinned_settings(attempt, revision)
    limits = (
        ScreeningReviewBudgetLimits(
            settings_revision=revision.revision,
            settings_scope=revision.scope,
            max_input_tokens=settings.max_input_tokens,
            max_raw_input_tokens=MAX_AGGREGATE_RAW_INPUT_TOKENS,
            max_output_tokens=settings.max_output_tokens,
            max_cost_usd=settings.max_cost_usd,
            l1_max_steps=settings.source_review_max_steps,
            l1_timeout_seconds=settings.source_review_timeout_seconds,
            l2_max_steps=settings.max_steps,
            l2_timeout_seconds=settings.timeout_seconds,
            adjudicator_max_steps=settings.adjudicator_max_steps,
            adjudicator_timeout_seconds=settings.adjudicator_timeout_seconds,
        )
        if settings is not None and revision is not None
        else None
    )

    budget_stop: ScreeningReviewBudgetStopView | None = None
    if usage is not None and isinstance(usage.budget_stop, dict):
        try:
            budget_stop = ScreeningReviewBudgetStopView.model_validate(
                usage.budget_stop
            )
        except ValidationError:
            logger.warning("invalid stored budget stop attempt_id=%s", attempt_id)
    raw_stages = [
        row
        for row in (usage.stages if usage is not None else []) or []
        if isinstance(row, dict)
    ]
    stages = [
        ScreeningReviewBudgetStage(
            stage=str(row.get("stage", "unknown")),
            model=str(row.get("model", "unknown")),
            requests=_int(row.get("requests")),
            input_tokens=_int(row.get("input_tokens")),
            cached_input_tokens=_int(row.get("cached_input_tokens")),
            output_tokens=_int(row.get("output_tokens")),
            reasoning_tokens=_int(row.get("reasoning_tokens")),
            estimated_cost_usd=_cost(row.get("estimated_cost_usd")) or 0.0,
            reported_cost_usd=_cost(row.get("reported_cost_usd")),
            wall_ms=(
                _int(row.get("wall_ms")) if row.get("wall_ms") is not None else None
            ),
            tripped=(
                list(budget_stop.tripped)
                if budget_stop is not None and budget_stop.stage == row.get("stage")
                else []
            ),
        )
        for row in raw_stages
    ]
    aggregate = _l2_aggregate(raw_stages)
    finished = attempt.finished_at
    return ScreeningReviewBudgetResponse(
        attempt_id=attempt.attempt_id,
        agent_id=attempt.agent_id,
        status=attempt.status,
        reason_code=attempt.reason_code,
        build_only=attempt.build_only,
        started_at=attempt.started_at,
        finished_at=finished,
        wall_seconds=(
            round((finished - attempt.started_at).total_seconds(), 3)
            if finished is not None
            else None
        ),
        telemetry_recorded=usage is not None,
        recorded_at=usage.recorded_at if usage is not None else None,
        review_elapsed_ms=usage.elapsed_ms if usage is not None else None,
        total_input_tokens=usage.total_input_tokens if usage is not None else None,
        total_output_tokens=usage.total_output_tokens if usage is not None else None,
        total_cost_usd=_float(usage.total_cost_usd) if usage is not None else None,
        total_estimated_cost_usd=(
            round(sum(stage.estimated_cost_usd for stage in stages), 6)
            if usage is not None
            else None
        ),
        stages=stages,
        l2_aggregate=aggregate,
        limits=limits,
        exceeded_limits=_exceeded(aggregate, limits),
        budget_stop=budget_stop,
        lineage=ScreeningReviewBudgetLineage(
            parent_agent_id=attempt.lineage_parent_agent_id,
            parent_name=parent.name if parent is not None else None,
            parent_version=parent.version if parent is not None else None,
            scope=attempt.lineage_scope,  # type: ignore[arg-type]
            full_review_reasons=attempt.lineage_full_review_reasons,
            reference_kind=attempt.lineage_reference_kind,
            packet_sha256=attempt.lineage_packet_sha256,
            changed_file_count=attempt.lineage_changed_file_count,
            changed_line_count=attempt.lineage_changed_line_count,
        ),
    )

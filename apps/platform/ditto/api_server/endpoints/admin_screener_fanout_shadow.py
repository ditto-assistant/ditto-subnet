"""Read-only Backroom view of two-stage screener shadow comparisons."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_fanout_shadow import (
    AdminFanoutShadowMetrics,
    AdminFanoutShadowResponse,
    AdminFanoutShadowReview,
    FanoutShadowOutcome,
    FanoutShadowStatus,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import ScreenerFanoutShadowReview

router = APIRouter(prefix="/admin/screener-fanout-shadow", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _view(row: ScreenerFanoutShadowReview) -> AdminFanoutShadowReview:
    return AdminFanoutShadowReview(
        shadow_id=row.shadow_id,
        agent_id=row.agent_id,
        attempt_id=row.attempt_id,
        artifact_sha256=row.artifact_sha256,
        policy_version=row.policy_version,
        policy_manifest_profile=cast(
            Literal["core", "l1", "l1_l2"], row.policy_manifest_profile
        ),
        policy_manifest_rotation_id=row.policy_manifest_rotation_id,
        policy_manifest_digest=row.policy_manifest_digest,
        settings_revision=row.settings_revision,
        settings_scope=row.settings_scope,
        settings_checksum=row.settings_checksum,
        status=cast(FanoutShadowStatus, row.status),
        outcome=cast(FanoutShadowOutcome | None, row.outcome),
        baseline=row.baseline,
        report=row.report,
        disagrees_with_baseline=row.disagrees_with_baseline,
        coverage_complete=row.coverage_complete,
        error_code=row.error_code,
        provider=row.provider,
        reserved_cost_usd=row.reserved_cost_microusd / 1_000_000,
        reported_cost_usd=(
            row.reported_cost_microusd / 1_000_000
            if row.reported_cost_microusd is not None
            else None
        ),
        unmetered=row.unmetered,
        reserved_at=row.reserved_at,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router.get("", response_model=AdminFanoutShadowResponse)
async def get_screener_fanout_shadow(
    _admin: AdminDep,
    session: SessionDep,
    status: Literal["queued", "leased", "running", "succeeded", "incomplete", "skipped"]
    | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminFanoutShadowResponse:
    """Return exact baseline/fan-out pairs and bounded fleet-wide metrics."""
    predicates = (
        [ScreenerFanoutShadowReview.status == status] if status is not None else []
    )
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreenerFanoutShadowReview)
            .where(*predicates)
        )
        or 0
    )
    rows = list(
        await session.scalars(
            select(ScreenerFanoutShadowReview)
            .where(*predicates)
            .order_by(
                ScreenerFanoutShadowReview.created_at.desc(),
                ScreenerFanoutShadowReview.shadow_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )
    status_counts = {
        value: int(count)
        for value, count in (
            await session.execute(
                select(
                    ScreenerFanoutShadowReview.status,
                    func.count(ScreenerFanoutShadowReview.shadow_id),
                ).group_by(ScreenerFanoutShadowReview.status)
            )
        ).all()
    }
    compared = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreenerFanoutShadowReview)
            .where(ScreenerFanoutShadowReview.report.is_not(None))
        )
        or 0
    )
    disagreements = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreenerFanoutShadowReview)
            .where(ScreenerFanoutShadowReview.disagrees_with_baseline.is_(True))
        )
        or 0
    )
    incomplete_coverage = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreenerFanoutShadowReview)
            .where(ScreenerFanoutShadowReview.coverage_complete.is_(False))
        )
        or 0
    )
    since = datetime.now(UTC) - timedelta(hours=24)
    reserved, reported, unmetered = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(ScreenerFanoutShadowReview.reserved_cost_microusd), 0
                ),
                func.coalesce(
                    func.sum(ScreenerFanoutShadowReview.reported_cost_microusd), 0
                ),
                func.count(ScreenerFanoutShadowReview.shadow_id).filter(
                    ScreenerFanoutShadowReview.unmetered.is_(True)
                ),
            ).where(ScreenerFanoutShadowReview.reserved_at >= since)
        )
    ).one()
    metrics = AdminFanoutShadowMetrics(
        total=sum(status_counts.values()),
        queued=status_counts.get("queued", 0),
        running=status_counts.get("leased", 0) + status_counts.get("running", 0),
        succeeded=status_counts.get("succeeded", 0),
        incomplete=status_counts.get("incomplete", 0),
        skipped=status_counts.get("skipped", 0),
        compared=compared,
        disagreements=disagreements,
        incomplete_coverage=incomplete_coverage,
        rolling_24h_reserved_cost_usd=int(reserved) / 1_000_000,
        rolling_24h_reported_cost_usd=int(reported) / 1_000_000,
        rolling_24h_unmetered=int(unmetered),
    )
    return AdminFanoutShadowResponse(
        metrics=metrics,
        items=[_view(row) for row in rows],
        count=total,
        returned=len(rows),
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )

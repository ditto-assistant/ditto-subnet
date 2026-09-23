"""Backroom read of the ordinary source-review queue-age SLO (ditto-subnet#2042).

Vertical slice 1: ordinary (pre-score) screening review only. See
``ditto.db.queries.source_review_queue_slo`` for the class boundary, clock,
and reason definitions this endpoint reports, and for why top-agent, copy,
ATH, and human-escalation review are explicitly out of scope here.

Read-only by design: no threshold enforcement, no alert, no operator
escalation action. Those are explicit ditto-subnet#2042 follow-ups.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.source_review_queue_slo import SourceReviewQueueSlo
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.source_review_queue_slo import (
    load_source_review_queue_slo_snapshot,
)

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/admin/source-review-queue-slo",
    response_model=SourceReviewQueueSlo,
)
async def get_source_review_queue_slo(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> SourceReviewQueueSlo:
    """p50/p95/oldest age, throughput, and reconciliation ghosts."""
    slo_config = request.app.state.config.source_review_queue_slo
    snapshot = await load_source_review_queue_slo_snapshot(
        session,
        max_actionable_age_threshold_seconds=(
            slo_config.max_actionable_age_threshold_seconds
        ),
        p95_age_threshold_seconds=slo_config.p95_age_threshold_seconds,
    )
    return SourceReviewQueueSlo(
        generated_at=datetime.now(UTC),
        backlog_count=snapshot.backlog_count,
        active_work_count=snapshot.active_work_count,
        capacity_wait_count=snapshot.capacity_wait_count,
        infrastructure_backoff_count=snapshot.infrastructure_backoff_count,
        escalation_count=snapshot.escalation_count,
        p50_age_seconds=snapshot.p50_age_seconds,
        p95_age_seconds=snapshot.p95_age_seconds,
        oldest_age_seconds=snapshot.oldest_age_seconds,
        throughput_window_hours=snapshot.throughput_window_hours,
        throughput_completed_count=snapshot.throughput_completed_count,
        throughput_per_hour=snapshot.throughput_per_hour,
        stale_running_ghost_count=snapshot.stale_running_ghost_count,
        resolved_quarantine_ghost_count=snapshot.resolved_quarantine_ghost_count,
        attempt_status_drift_ghost_count=snapshot.attempt_status_drift_ghost_count,
        ghost_count=snapshot.ghost_count,
        max_actionable_age_threshold_seconds=(
            snapshot.max_actionable_age_threshold_seconds
        ),
        overdue_count=snapshot.overdue_count,
        p95_age_threshold_seconds=snapshot.p95_age_threshold_seconds,
        p95_exceeds_threshold=snapshot.p95_exceeds_threshold,
    )

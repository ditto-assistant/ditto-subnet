"""Backroom read of the ATH-hold queue-age SLO (ditto-subnet#2042, slice 2).

This covers every pending ``ath_reviews`` hold, broken down by queue class,
with copy review as its own clock. See
``ditto.db.queries.ath_review_queue_slo`` for the class, clock, reason, and
ghost definitions.

It is read-only by design. It does not enforce thresholds, raise alerts, or
take escalation actions. Those remain #2042 follow-ups.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ath_review_queue_slo import (
    AthReviewQueueSlo,
    AthReviewQueueStats,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.ath_review_queue_slo import (
    AthReviewQueueStats as AthReviewQueueStatsSnapshot,
)
from ditto.db.queries.ath_review_queue_slo import (
    load_ath_review_queue_slo_snapshot,
)

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _stats(stats: AthReviewQueueStatsSnapshot) -> AthReviewQueueStats:
    return AthReviewQueueStats(
        queue_class=stats.queue_class,
        backlog_count=stats.backlog_count,
        active_work_count=stats.active_work_count,
        capacity_wait_count=stats.capacity_wait_count,
        infrastructure_backoff_count=stats.infrastructure_backoff_count,
        escalation_count=stats.escalation_count,
        p50_age_seconds=stats.p50_age_seconds,
        p95_age_seconds=stats.p95_age_seconds,
        oldest_age_seconds=stats.oldest_age_seconds,
        oldest_agent_id=stats.oldest_agent_id,
        oldest_reason=stats.oldest_reason,
        throughput_window_hours=stats.throughput_window_hours,
        throughput_completed_count=stats.throughput_completed_count,
        throughput_per_hour=stats.throughput_per_hour,
        max_actionable_age_threshold_seconds=(
            stats.max_actionable_age_threshold_seconds
        ),
        overdue_count=stats.overdue_count,
        p95_age_threshold_seconds=stats.p95_age_threshold_seconds,
        p95_exceeds_threshold=stats.p95_exceeds_threshold,
    )


@router.get("/admin/ath-review-queue-slo", response_model=AthReviewQueueSlo)
async def get_ath_review_queue_slo(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> AthReviewQueueSlo:
    """ATH and copy-review queue age, throughput, overdue counts, and ghosts."""
    slo_config = request.app.state.config.ath_review_queue_slo
    snapshot = await load_ath_review_queue_slo_snapshot(
        session,
        ath_thresholds=slo_config.ath,
        copy_thresholds=slo_config.copy,
    )
    return AthReviewQueueSlo(
        generated_at=datetime.now(UTC),
        ath=_stats(snapshot.ath),
        copy_review=_stats(snapshot.copy),
        classes=[_stats(stats) for stats in snapshot.classes],
        stranded_terminal_ghost_count=snapshot.stranded_terminal_ghost_count,
        stranded_hold_ghost_count=snapshot.stranded_hold_ghost_count,
        held_without_review_ghost_count=snapshot.held_without_review_ghost_count,
        ghost_count=snapshot.ghost_count,
    )

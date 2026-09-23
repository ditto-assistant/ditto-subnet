"""Operator model for the ordinary source-review queue-age SLO.

Vertical slice 1 of ditto-subnet#2042 (ordinary review only; see
``ditto.db.queries.source_review_queue_slo`` for the class-boundary
definition and predicates this model reports).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SourceReviewQueueSlo(BaseModel):
    """p50/p95/oldest age, throughput, overdue, and reconciliation ghosts.

    Every age/threshold field is seconds. ``overdue_count`` and
    ``p95_exceeds_threshold`` are ``null`` whenever their governing
    threshold is unset -- never ``0`` and never a computed "healthy"
    default. This endpoint is read-only: it enforces nothing (no alert, no
    operator escalation action -- both are explicit ditto-subnet#2042
    follow-ups).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime

    backlog_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Current, non-superseded, non-terminal, non-progressed-past-"
                "screening items counted below. Terminal ghosts are excluded "
                "here and reported separately."
            ),
        ),
    ]
    active_work_count: Annotated[int, Field(ge=0)]
    capacity_wait_count: Annotated[int, Field(ge=0)]
    infrastructure_backoff_count: Annotated[int, Field(ge=0)]
    escalation_count: Annotated[int, Field(ge=0)]

    p50_age_seconds: Annotated[
        float | None,
        Field(
            default=None,
            ge=0,
            description="Median actionable age; null only when the backlog is empty.",
        ),
    ]
    p95_age_seconds: Annotated[float | None, Field(default=None, ge=0)]
    oldest_age_seconds: Annotated[
        float | None,
        Field(
            default=None, ge=0, description="Age of the single oldest actionable item."
        ),
    ]

    throughput_window_hours: Annotated[int, Field(gt=0)]
    throughput_completed_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Full (non-build-only) screening attempts reaching a passed or "
                "rejected verdict within the throughput window."
            ),
        ),
    ]
    throughput_per_hour: Annotated[float, Field(ge=0)]

    stale_running_ghost_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Agents whose latest screening attempt still looks 'running' "
                "although the agent already reached a terminal or progressed-"
                "past-screening status. Visible for reconciliation; never "
                "folded into the counts above. See ditto-subnet#2038."
            ),
        ),
    ]
    resolved_quarantine_ghost_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Agents stuck at quarantined status with no active quarantine "
                "row (a resolved quarantine that did not flip agent status)."
            ),
        ),
    ]
    ghost_count: Annotated[
        int, Field(ge=0, description="Sum of the two reconciliation counts above.")
    ]

    max_actionable_age_threshold_seconds: Annotated[
        int | None,
        Field(
            default=None,
            gt=0,
            description=(
                "Configured overdue threshold, or null when unset. Observability"
                "-only: not enforced by this endpoint."
            ),
        ),
    ]
    overdue_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Actionable items older than the threshold; null when unset.",
        ),
    ]
    p95_age_threshold_seconds: Annotated[int | None, Field(default=None, gt=0)]
    p95_exceeds_threshold: Annotated[
        bool | None,
        Field(default=None, description="Null unless a p95 threshold is configured."),
    ]

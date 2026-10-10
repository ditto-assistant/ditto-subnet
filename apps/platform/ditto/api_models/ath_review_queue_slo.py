"""Operator model for the ATH-hold queue-age SLO, copy review included.

Vertical slice 2 of ditto-subnet#2042. ``ditto.db.queries.ath_review_queue_slo``
defines the queue classes, clock, reasons, and reconciliation ghosts reported
here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

AthReviewQueueClass = Literal[
    "copy",
    "benchmark_overfit",
    "deferred_source_review",
    "integrity_double_check",
    "anomalous_score",
]
AthReviewHoldReason = Literal[
    "active_work", "capacity_wait", "infrastructure_backoff", "escalation"
]


class AthReviewQueueStats(BaseModel):
    """p50, p95, and oldest age, throughput, and overdue count for one clock.

    Every age and threshold field is in seconds. ``overdue_count`` and
    ``p95_exceeds_threshold`` are ``null`` whenever their threshold is
    unset, never ``0`` and never a computed "healthy" default.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    queue_class: Annotated[
        AthReviewQueueClass | None,
        Field(
            description=(
                "Null for the aggregate over every pending ATH hold. "
                "integrity_double_check is the top-five deferred_source_review "
                "hold; filter the copy-review queue with "
                "review_kind=deferred_source_review to list it."
            ),
        ),
    ]
    backlog_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Pending holds whose agent is still ath_pending_review. "
                "Stranded holds are reported separately as ghosts."
            ),
        ),
    ]
    active_work_count: Annotated[int, Field(ge=0)]
    capacity_wait_count: Annotated[int, Field(ge=0)]
    infrastructure_backoff_count: Annotated[int, Field(ge=0)]
    escalation_count: Annotated[
        int,
        Field(ge=0, description="Holds waiting on an operator decision."),
    ]

    p50_age_seconds: Annotated[
        float | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Median age since the hold last became pending "
                "(COALESCE(reopened_at, opened_at)). Null only when the "
                "backlog is empty."
            ),
        ),
    ]
    p95_age_seconds: Annotated[float | None, Field(default=None, ge=0)]
    oldest_age_seconds: Annotated[float | None, Field(default=None, ge=0)]
    oldest_agent_id: Annotated[
        UUID | None,
        Field(
            default=None,
            description="Agent behind the oldest actionable hold (operator-only).",
        ),
    ]
    oldest_reason: Annotated[AthReviewHoldReason | None, Field(default=None)]

    throughput_window_hours: Annotated[int, Field(gt=0)]
    throughput_completed_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Reviews of this class resolved (clear or reject) within the "
                "window. Counted by the review's latest resolved_at."
            ),
        ),
    ]
    throughput_per_hour: Annotated[float, Field(ge=0)]

    max_actionable_age_threshold_seconds: Annotated[
        int | None,
        Field(
            default=None,
            gt=0,
            description="Configured overdue threshold, or null when unset.",
        ),
    ]
    overdue_count: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Actionable holds older than the threshold; null when unset.",
        ),
    ]
    p95_age_threshold_seconds: Annotated[int | None, Field(default=None, gt=0)]
    p95_exceeds_threshold: Annotated[
        bool | None,
        Field(default=None, description="Null unless a p95 threshold is configured."),
    ]


class AthReviewQueueSlo(BaseModel):
    """ATH-hold queue age against SLO: the aggregate, each class, and ghosts.

    Read-only. It enforces nothing: there is no alert and no operator
    escalation action. Both are ditto-subnet#2042 follow-ups.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    ath: Annotated[
        AthReviewQueueStats,
        Field(description="Every pending ATH hold: the ATH review clock."),
    ]
    copy_review: Annotated[
        AthReviewQueueStats,
        Field(
            description=(
                "The copy-review clock (review_kind=copy, including legacy "
                "holds with no kind). The same entry appears in classes."
            ),
        ),
    ]
    classes: Annotated[
        list[AthReviewQueueStats],
        Field(description="One entry per queue class, always all present."),
    ]
    stranded_terminal_ghost_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Pending reviews whose agent is already banned or rejected. "
                "Terminal history, never backlog."
            ),
        ),
    ]
    stranded_hold_ghost_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Pending reviews whose agent moved to another non-held status. "
                "Needs unsticking; resolving it through the API answers 409."
            ),
        ),
    ]
    held_without_review_ghost_count: Annotated[
        int,
        Field(
            ge=0,
            description="ath_pending_review agents with no pending review row.",
        ),
    ]
    ghost_count: Annotated[
        int, Field(ge=0, description="Sum of the three reconciliation counts above.")
    ]

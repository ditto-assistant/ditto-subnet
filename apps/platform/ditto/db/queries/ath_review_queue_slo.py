"""Actionable-age SLO reads for the ATH-hold queue, copy review included.

Vertical slice 2 of ditto-subnet#2042. Slice 1
(:mod:`ditto.db.queries.source_review_queue_slo`) covers ordinary pre-score
screening review. This module covers the post-score holds: every
``ath_reviews`` row with ``status = 'pending'``, which
``docs/ath-review-queue.md`` defines as the operator queue. Copy review is one
class of that queue, not a separate table.

Queue classes
-------------

``ath_reviews`` keeps the hold's kind in ``algorithm_provenance``. This module
derives a :data:`AthQueueClass` from it:

* ``copy`` -- ``review_kind = 'copy'``, plus a missing or unrecognized kind.
  That is the same fallback ``admin_copy_review._review_kind_filter`` and the
  queue projection use, so a legacy hold counts in the class the queue already
  shows it in.
* ``benchmark_overfit`` -- the transform audit.
* ``deferred_source_review`` -- score-qualified post-score deep source review.
* ``integrity_double_check`` -- the stronger top-five review. It is a
  ``deferred_source_review`` hold with
  ``algorithm_provenance.trigger = 'integrity_double_check'``. It shares that
  lifecycle, but its posture and capacity differ, so it gets its own clock.
* ``anomalous_score`` -- the out-of-band composite escalation.

Clock
-----

``age_seconds`` runs from ``COALESCE(reopened_at, opened_at)``, the instant
the hold most recently became pending. The queue endpoint orders by the same
expression, and ``claim_screening_attempts`` uses it to decide whether a deep
pass has started for this hold. A deep-review retry (a new
``screening_attempts`` row) never moves it. Only an operator reopen of a
resolved review restarts it, because a resolved review is not in the queue
until the reopen.

Reasons
-------

Every actionable hold gets exactly one reason (see
:func:`classify_ath_hold_reason`):

* ``copy``, ``benchmark_overfit``, and ``anomalous_score`` holds are always
  ``escalation``. Only an operator resolution exits them. The copy court's
  recommendation is advisory and does not change the reason.
* ``deferred_source_review`` and ``integrity_double_check`` holds wait on a
  screener deep pass first. The newest screening attempt started since the
  hold's clock began decides the reason. No attempt means ``capacity_wait``.
  A ``running`` attempt means ``active_work``. A ``failed`` or ``expired``
  attempt means ``infrastructure_backoff``, parked until an operator
  authorizes one exact retry. Any concluded attempt means ``escalation``: the
  automated pass finished and left the hold for an operator.

Reconciliation ghosts
---------------------

``agents.status`` and ``ath_reviews.status`` live in separate tables and can
disagree (``docs/ath-review-queue.md``). A pending review whose agent is no
longer ``ath_pending_review`` is not queue work, and resolving it answers
409. These rows stay visible but never count toward backlog, percentiles,
oldest age, or overdue counts. There are three kinds:

* ``stranded_terminal_ghost_count`` -- a pending review whose agent is
  already ``banned`` or ``rejected``. This is terminal history.
* ``stranded_hold_ghost_count`` -- a pending review whose agent moved to any
  other non-held status, for example by the legacy ``resolve_review()`` CLI
  or the deep-review park, release, and rescore path.
* ``held_without_review_ghost_count`` -- an ``ath_pending_review`` agent with
  no pending review row.

Aggregation
-----------

The pending ATH queue is small, at most one row per held agent. This module
reads the classified rows in one bounded statement and computes percentiles
in Python with :func:`percentile_cont`. That function uses the same linear
interpolation as PostgreSQL's ``percentile_cont``, so slice 1 and this slice
report comparable numbers. The age and breach math stays pure and
unit-testable.

Thresholds are inputs and default to unset, as in slice 1. An unset
threshold reports ``overdue_count`` and ``p95_exceeds_threshold`` as
``None``, never ``0`` or "healthy". The ATH aggregate and the copy class each
take their own thresholds. The other classes report null thresholds. Nothing
here alerts or escalates. Both remain #2042 follow-ups.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args
from uuid import UUID

from sqlalchemy import text

from ditto.api_models.agent_status import AgentStatus
from ditto.db.queries.terminal_quarantine_reconciliation import (
    TERMINAL_QUARANTINE_AGENT_STATUSES,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

AthQueueClass = Literal[
    "copy",
    "benchmark_overfit",
    "deferred_source_review",
    "integrity_double_check",
    "anomalous_score",
]
ATH_QUEUE_CLASSES: tuple[AthQueueClass, ...] = get_args(AthQueueClass)
"""Display order of the per-class breakdown. Every class is always reported,
even with an empty backlog, so a missing class never reads as healthy."""

AthHoldReason = Literal[
    "active_work", "capacity_wait", "infrastructure_backoff", "escalation"
]

# The same four kinds ``admin_copy_review._KNOWN_REVIEW_KINDS`` recognizes.
# Anything else, including a missing key, is a legacy copy hold.
_KNOWN_REVIEW_KINDS = frozenset(
    {"copy", "benchmark_overfit", "deferred_source_review", "anomalous_score"}
)
_DEFERRED_REVIEW_KIND = "deferred_source_review"
# Mirrors ``ditto.api_server.deferred_source_review.INTEGRITY_DOUBLE_CHECK_TRIGGER``
# without importing the API layer into queries (the same pattern
# ``ditto.db.queries.screening`` uses).
_INTEGRITY_DOUBLE_CHECK_TRIGGER = "integrity_double_check"
_DEEP_PASS_QUEUE_CLASSES: frozenset[AthQueueClass] = frozenset(
    {"deferred_source_review", "integrity_double_check"}
)
_TERMINAL_AGENT_STATUS_VALUES = frozenset(
    status.value for status in TERMINAL_QUARANTINE_AGENT_STATUSES
)

DEFAULT_THROUGHPUT_WINDOW_HOURS = 24
"""Observation window for the resolution rate. This is not a policy
deadline. It matches slice 1's window so the two snapshots line up."""


@dataclass(frozen=True)
class QueueAgeThresholds:
    """Observability-only thresholds for one queue clock. Both default unset."""

    max_actionable_age_seconds: int | None = None
    p95_age_seconds: int | None = None


_UNSET_THRESHOLDS = QueueAgeThresholds()


def normalize_review_kind(raw: object) -> str:
    """The stored ``review_kind``, with the queue's legacy ``copy`` fallback."""
    return raw if isinstance(raw, str) and raw in _KNOWN_REVIEW_KINDS else "copy"


def ath_queue_class(review_kind: object, trigger: object) -> AthQueueClass:
    """The SLO clock a pending hold reports under."""
    kind = normalize_review_kind(review_kind)
    if kind == _DEFERRED_REVIEW_KIND:
        if trigger == _INTEGRITY_DOUBLE_CHECK_TRIGGER:
            return "integrity_double_check"
        return "deferred_source_review"
    if kind == "benchmark_overfit":
        return "benchmark_overfit"
    if kind == "anomalous_score":
        return "anomalous_score"
    return "copy"


def classify_ath_hold_reason(
    queue_class: AthQueueClass, attempt_status_since_hold: str | None
) -> AthHoldReason:
    """Why an actionable hold is waiting. See the module docstring."""
    if queue_class not in _DEEP_PASS_QUEUE_CLASSES:
        return "escalation"
    if attempt_status_since_hold is None:
        return "capacity_wait"
    if attempt_status_since_hold == "running":
        return "active_work"
    if attempt_status_since_hold in ("failed", "expired"):
        return "infrastructure_backoff"
    return "escalation"


def percentile_cont(sorted_values: Sequence[float], fraction: float) -> float | None:
    """PostgreSQL ``percentile_cont`` over an already-sorted sequence.

    Returns ``None`` for an empty sequence. An empty backlog has no age to
    report, and that is not the same as an age of zero.
    """
    if not sorted_values:
        return None
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(f"fraction must be within [0, 1], got {fraction}")
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    low_value = float(sorted_values[lower])
    if lower == upper:
        return low_value
    return low_value + (float(sorted_values[upper]) - low_value) * (position - lower)


@dataclass(frozen=True)
class AthHoldRow:
    """One pending ``ath_reviews`` row with its derived class, reason, and age.

    ``reason`` is ``None`` exactly when the row is a stranded-hold
    reconciliation ghost, meaning its agent is no longer
    ``ath_pending_review``.
    """

    review_id: UUID
    agent_id: UUID
    agent_status: str
    queue_class: AthQueueClass
    age_seconds: float
    reason: AthHoldReason | None

    @property
    def actionable(self) -> bool:
        return self.reason is not None

    @property
    def stranded_terminal(self) -> bool:
        return (
            self.reason is None and self.agent_status in _TERMINAL_AGENT_STATUS_VALUES
        )


def build_ath_hold_row(
    *,
    review_id: UUID,
    agent_id: UUID,
    agent_status: str,
    review_kind: object,
    trigger: object,
    age_seconds: float,
    attempt_status_since_hold: str | None,
) -> AthHoldRow:
    """Classify one raw pending-review row. This is pure and has no I/O."""
    queue_class = ath_queue_class(review_kind, trigger)
    reason = (
        classify_ath_hold_reason(queue_class, attempt_status_since_hold)
        if agent_status == AgentStatus.ATH_PENDING_REVIEW.value
        else None
    )
    return AthHoldRow(
        review_id=review_id,
        agent_id=agent_id,
        agent_status=agent_status,
        queue_class=queue_class,
        # Clock skew between the app and the database must not report a
        # negative age.
        age_seconds=max(0.0, float(age_seconds)),
        reason=reason,
    )


@dataclass(frozen=True)
class AthReviewQueueStats:
    """Queue-age statistics for one clock: the ATH aggregate or one class.

    ``queue_class`` is ``None`` for the aggregate over every class. Age fields
    are seconds. Percentile and oldest fields are ``None`` only when the
    backlog is empty. ``overdue_count`` and ``p95_exceeds_threshold`` are
    ``None`` whenever their threshold is unset.
    """

    queue_class: AthQueueClass | None
    backlog_count: int
    active_work_count: int
    capacity_wait_count: int
    infrastructure_backoff_count: int
    escalation_count: int
    p50_age_seconds: float | None
    p95_age_seconds: float | None
    oldest_age_seconds: float | None
    oldest_agent_id: UUID | None
    oldest_reason: AthHoldReason | None
    throughput_window_hours: int
    throughput_completed_count: int
    throughput_per_hour: float
    max_actionable_age_threshold_seconds: int | None
    overdue_count: int | None
    p95_age_threshold_seconds: int | None
    p95_exceeds_threshold: bool | None


def summarize_ath_queue(
    rows: Iterable[AthHoldRow],
    *,
    queue_class: AthQueueClass | None,
    throughput_completed_count: int,
    throughput_window_hours: int,
    thresholds: QueueAgeThresholds | None = None,
) -> AthReviewQueueStats:
    """Aggregate the actionable rows of one clock. This is pure and has no I/O.

    ``rows`` may contain ghosts and other classes. Only actionable rows of
    ``queue_class`` (or every class when ``None``) are counted.
    """
    if throughput_window_hours <= 0:
        raise ValueError("throughput_window_hours must be positive")
    actionable = [
        row
        for row in rows
        if row.reason is not None
        and (queue_class is None or row.queue_class == queue_class)
    ]
    ages = sorted(row.age_seconds for row in actionable)
    oldest = max(
        actionable,
        key=lambda row: (row.age_seconds, str(row.review_id)),
        default=None,
    )
    p95_age_seconds = percentile_cont(ages, 0.95)
    thresholds = thresholds or _UNSET_THRESHOLDS
    max_age = thresholds.max_actionable_age_seconds
    p95_threshold = thresholds.p95_age_seconds
    return AthReviewQueueStats(
        queue_class=queue_class,
        backlog_count=len(actionable),
        active_work_count=sum(row.reason == "active_work" for row in actionable),
        capacity_wait_count=sum(row.reason == "capacity_wait" for row in actionable),
        infrastructure_backoff_count=sum(
            row.reason == "infrastructure_backoff" for row in actionable
        ),
        escalation_count=sum(row.reason == "escalation" for row in actionable),
        p50_age_seconds=percentile_cont(ages, 0.5),
        p95_age_seconds=p95_age_seconds,
        oldest_age_seconds=oldest.age_seconds if oldest is not None else None,
        oldest_agent_id=oldest.agent_id if oldest is not None else None,
        oldest_reason=oldest.reason if oldest is not None else None,
        throughput_window_hours=throughput_window_hours,
        throughput_completed_count=throughput_completed_count,
        throughput_per_hour=throughput_completed_count / throughput_window_hours,
        max_actionable_age_threshold_seconds=max_age,
        # Strictly older than the threshold, the same comparison as slice 1.
        overdue_count=(None if max_age is None else sum(age > max_age for age in ages)),
        p95_age_threshold_seconds=p95_threshold,
        p95_exceeds_threshold=(
            None
            if p95_threshold is None or p95_age_seconds is None
            else p95_age_seconds > p95_threshold
        ),
    )


@dataclass(frozen=True)
class AthReviewQueueSloSnapshot:
    """One observability read over the pending ATH-hold queue."""

    throughput_window_hours: int
    ath: AthReviewQueueStats
    classes: tuple[AthReviewQueueStats, ...]
    stranded_terminal_ghost_count: int
    stranded_hold_ghost_count: int
    held_without_review_ghost_count: int

    def for_class(self, queue_class: AthQueueClass) -> AthReviewQueueStats:
        """The clock for one class. Every class is always present."""
        return next(stats for stats in self.classes if stats.queue_class == queue_class)

    @property
    def copy(self) -> AthReviewQueueStats:
        """The copy-review clock."""
        return self.for_class("copy")

    @property
    def ghost_count(self) -> int:
        return (
            self.stranded_terminal_ghost_count
            + self.stranded_hold_ghost_count
            + self.held_without_review_ghost_count
        )


def build_ath_review_queue_slo_snapshot(
    rows: Sequence[AthHoldRow],
    *,
    throughput_by_class: Mapping[AthQueueClass, int],
    held_without_review_ghost_count: int,
    throughput_window_hours: int = DEFAULT_THROUGHPUT_WINDOW_HOURS,
    ath_thresholds: QueueAgeThresholds | None = None,
    copy_thresholds: QueueAgeThresholds | None = None,
) -> AthReviewQueueSloSnapshot:
    """Assemble the snapshot from classified rows. This is pure and has no I/O."""
    classes = tuple(
        summarize_ath_queue(
            rows,
            queue_class=queue_class,
            throughput_completed_count=throughput_by_class.get(queue_class, 0),
            throughput_window_hours=throughput_window_hours,
            thresholds=copy_thresholds if queue_class == "copy" else None,
        )
        for queue_class in ATH_QUEUE_CLASSES
    )
    ghosts = [row for row in rows if row.reason is None]
    stranded_terminal = sum(row.stranded_terminal for row in ghosts)
    return AthReviewQueueSloSnapshot(
        throughput_window_hours=throughput_window_hours,
        ath=summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=sum(throughput_by_class.values()),
            throughput_window_hours=throughput_window_hours,
            thresholds=ath_thresholds,
        ),
        classes=classes,
        stranded_terminal_ghost_count=stranded_terminal,
        stranded_hold_ghost_count=len(ghosts) - stranded_terminal,
        held_without_review_ghost_count=held_without_review_ghost_count,
    )


# ``ath_reviews_agent_id_key`` makes this one row per agent. The LATERAL picks
# the newest attempt started since the hold's clock began. That is the same
# ``started_at >= COALESCE(reopened_at, opened_at)`` boundary
# ``claim_screening_attempts`` uses to decide whether a deep pass has begun,
# with the same ``started_at DESC, attempt_id DESC`` tiebreak as
# ``latest_screening_attempt_id``.
_PENDING_HOLDS_SQL = """
SELECT r.review_id,
       r.agent_id,
       a.status::text AS agent_status,
       r.algorithm_provenance ->> 'review_kind' AS review_kind,
       r.algorithm_provenance ->> 'trigger' AS trigger,
       extract(epoch FROM now() - COALESCE(r.reopened_at, r.opened_at))
           AS age_seconds,
       la.status AS attempt_status_since_hold
  FROM ath_reviews r
  JOIN agents a ON a.agent_id = r.agent_id
  LEFT JOIN LATERAL (
      SELECT sa.status
        FROM screening_attempts sa
       WHERE sa.agent_id = r.agent_id
         AND sa.started_at >= COALESCE(r.reopened_at, r.opened_at)
       ORDER BY sa.started_at DESC, sa.attempt_id DESC
       LIMIT 1
  ) la ON true
 WHERE r.status = 'pending'
   AND (CAST(:agent_id AS uuid) IS NULL OR r.agent_id = CAST(:agent_id AS uuid))
"""

_HELD_WITHOUT_REVIEW_SQL = """
SELECT count(*)::bigint
  FROM agents a
 WHERE a.status = 'ath_pending_review'
   AND NOT EXISTS (
       SELECT 1 FROM ath_reviews r
        WHERE r.agent_id = a.agent_id AND r.status = 'pending'
   )
"""

# One count per resolved review. ``resolved_at`` is the latest resolution, so
# a review reopened and resolved again inside the window counts once. Every
# resolution path writes the row, including ones that skip
# ``ath_review_actions``.
_THROUGHPUT_SQL = """
SELECT r.algorithm_provenance ->> 'review_kind' AS review_kind,
       r.algorithm_provenance ->> 'trigger' AS trigger,
       count(*)::bigint AS completed
  FROM ath_reviews r
 WHERE r.status = 'resolved'
   AND r.resolved_at >= now() - make_interval(hours => :throughput_window_hours)
 GROUP BY 1, 2
"""


async def _load_pending_hold_rows(
    session: AsyncSession, *, agent_id: UUID | None
) -> list[AthHoldRow]:
    result = await session.execute(
        text(_PENDING_HOLDS_SQL),
        {"agent_id": agent_id},
    )
    return [
        build_ath_hold_row(
            review_id=row["review_id"],
            agent_id=row["agent_id"],
            agent_status=row["agent_status"],
            review_kind=row["review_kind"],
            trigger=row["trigger"],
            age_seconds=float(row["age_seconds"]),
            attempt_status_since_hold=row["attempt_status_since_hold"],
        )
        for row in result.mappings()
    ]


async def load_ath_review_queue_slo_snapshot(
    session: AsyncSession,
    *,
    throughput_window_hours: int = DEFAULT_THROUGHPUT_WINDOW_HOURS,
    ath_thresholds: QueueAgeThresholds | None = None,
    copy_thresholds: QueueAgeThresholds | None = None,
) -> AthReviewQueueSloSnapshot:
    """One bounded read over the pending ATH-hold queue's current state."""
    rows = await _load_pending_hold_rows(session, agent_id=None)
    held_without_review = int(await session.scalar(text(_HELD_WITHOUT_REVIEW_SQL)) or 0)
    throughput_by_class: dict[AthQueueClass, int] = {}
    throughput = await session.execute(
        text(_THROUGHPUT_SQL), {"throughput_window_hours": throughput_window_hours}
    )
    for row in throughput.mappings():
        queue_class = ath_queue_class(row["review_kind"], row["trigger"])
        throughput_by_class[queue_class] = throughput_by_class.get(
            queue_class, 0
        ) + int(row["completed"])
    return build_ath_review_queue_slo_snapshot(
        rows,
        throughput_by_class=throughput_by_class,
        held_without_review_ghost_count=held_without_review,
        throughput_window_hours=throughput_window_hours,
        ath_thresholds=ath_thresholds,
        copy_thresholds=copy_thresholds,
    )


@dataclass(frozen=True)
class AgentAthReviewState:
    """One agent's own ATH-hold clock, for the public projection.

    ``queue_class`` is for Platform-internal callers only. The public
    projection publishes the reason and age and never the class.
    """

    queue_class: AthQueueClass
    reason: AthHoldReason
    age_seconds: float


async def load_agent_ath_review_state(
    session: AsyncSession, *, agent_id: UUID
) -> AgentAthReviewState | None:
    """The agent's actionable ATH hold, or ``None``.

    The same statement and classification as the operator snapshot, filtered
    to one agent. ``None`` covers both "no pending hold" and a stranded-hold
    ghost. A ghost is not shown to a miner as active review, just as it is not
    counted for an operator.
    """
    rows = await _load_pending_hold_rows(session, agent_id=agent_id)
    row = next((candidate for candidate in rows if candidate.actionable), None)
    if row is None or row.reason is None:
        return None
    return AgentAthReviewState(
        queue_class=row.queue_class, reason=row.reason, age_seconds=row.age_seconds
    )

"""Actionable-age SLO reads for the ordinary (pre-score) source-review queue.

Scope note (ditto-subnet#2042, vertical slice 1 of 5): this module covers only
*ordinary* screening review -- the mechanical-admission-through-verdict
pipeline an agent sits in before it is ever scored. That is exactly
``AgentStatus`` in :data:`ORDINARY_REVIEW_ACTIONABLE_STATUSES`, one status per
actionable reason:

* ``uploaded``          -- no :class:`~ditto.db.models.ScreeningAttempt` has
  ever been claimed yet; the item is waiting on screener capacity.
* ``screening``         -- the current attempt is claimed and running.
* ``screening_failed``  -- the current attempt ended non-terminal
  (``retryable_infra``/``inconclusive``); fail-closed policy parks it until an
  operator authorizes one exact retry (see
  ``ditto.db.queries.screening_retry``). This is the "infrastructure backoff"
  reason: a technical parking state, not a content judgment.
* ``quarantined``       -- an active anti-cheat hold
  (:class:`~ditto.db.models.ScreeningQuarantine`, ``status='active'``); this
  is the "escalation" reason, a human judgment call on the artifact itself.

Stronger top-agent review, copy review, ATH review, and human escalation (the
other four review classes #2042 names) are explicitly OUT of scope here. They
live on ``AgentStatus.ATH_PENDING_REVIEW`` plus the ``ath_reviews`` table
(see ``ditto.api_server.deferred_source_review`` and
``ditto.api_server.outlier_escalation``), which is reachable ONLY from an
already-``SCORED``/``LIVE`` agent (verified against
``_evaluate_and_record_deferred_review`` and
``_evaluate_and_record_outlier_escalation`` in
``ditto.api_server.endpoints.validator``, both of which no-op unless
``agent.status in {SCORED, LIVE}``). Because that mechanism is keyed on a
completely different ``AgentStatus`` value reached from a completely
different code path, it can never appear in the query below: no filtering
against "top_five"/"copy"/"ath" trigger metadata is needed to keep this slice
"ordinary", because those other classes structurally cannot produce a row in
``ORDINARY_REVIEW_ACTIONABLE_STATUSES``. ATH review needs its own
review-state predicate over ``ath_reviews`` in a follow-up PR; this module
does not attempt one.

Current-attempt selection reuses the exact "latest, non-superseded attempt"
ordering as
:func:`ditto.db.queries.screening_retry.latest_screening_attempt_id`
(``ORDER BY started_at DESC, attempt_id DESC``), expressed here as a
``DISTINCT ON`` for a single bulk read instead of N correlated subqueries.

``age_seconds`` is deliberately the agent's own ``created_at`` -- the instant
it entered ordinary review -- not the latest attempt's ``started_at``. An
attempt's own start time resets on every retry/rescreen (a new
``ScreeningAttempt`` row, same agent), so keying the SLO clock on it let a
5-hour-old, repeatedly-retried submission report an age of a few seconds on
every fresh claim, hiding an overdue backlog item behind its own retry
history (Peyton's review on #2042). ``created_at`` never changes for a given
agent, so it is the one clock a retry cannot reset. ``current_attempt_age_seconds``
on :class:`AgentOrdinaryReviewState` separately exposes how long the CURRENT
attempt has been running/parked, for the (different) question of "is this
specific attempt stuck". The aggregate snapshot below does not carry a
percentile trio for that second clock -- only the per-agent read does --
since the primary defect this module reports on is backlog age, not
per-attempt duration; add one if that turns out to be needed too.

Escalation (an active anti-cheat quarantine) is classified FIRST, ahead of
whatever the latest attempt's own status says, matching
:func:`load_agent_ordinary_review_state` and its inline mirror in
``ditto.api_server.endpoints.public``: an active quarantine is a human
judgment call over the artifact regardless of what technical state the
underlying attempt happens to be in (a rescreen can fail, expire, or even
reach a verdict while the operator's hold is still open). Classifying by
attempt status first, as an earlier revision of this query did, let an
escalated row silently read as ``infrastructure_backoff`` -- and, worse, an
attempt status not covered by any bucket (e.g. a terminal ``passed``/
``rejected`` verdict on an agent whose own status never advanced) produced a
NULL reason that still counted toward ``backlog_count`` while summing to
none of the reason buckets. That drift is now its own reconciliation ghost,
:attr:`SourceReviewQueueSloSnapshot.attempt_status_drift_ghost_count`,
alongside the two #2038-owned ones below -- visible, but never actionable
backlog.

Reconciliation ("ghost") rows are visible but never counted toward the
actionable metrics -- see :class:`SourceReviewQueueSloSnapshot` for the
three kinds this module distinguishes, and the cross-link to #2038 below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select, text

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, ScreeningAttempt, ScreeningQuarantine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# The pre-score screening pipeline statuses that can carry an actionable
# ordinary-review clock. Order is display order, not filter order.
ORDINARY_REVIEW_ACTIONABLE_STATUSES = (
    AgentStatus.UPLOADED,
    AgentStatus.SCREENING,
    AgentStatus.SCREENING_FAILED,
    AgentStatus.QUARANTINED,
)

# Peyton's terminal/progressed-past exclusion list, verbatim. A ghost is a
# stale-looking active attempt whose agent has already reached one of these.
_GHOST_AGENT_STATUSES = (
    AgentStatus.REJECTED,
    AgentStatus.BANNED,
    AgentStatus.EVALUATING,
    AgentStatus.SCORED,
    AgentStatus.LIVE,
)

OrdinaryReviewReason = Literal[
    "active_work", "capacity_wait", "infrastructure_backoff", "escalation"
]

DEFAULT_THROUGHPUT_WINDOW_HOURS = 24
"""A fixed observation window for measuring completion rate, not a policy
deadline. Distinct from -- and not to be confused with -- the v13 screening
policy's "recommended 24 hours", which Peyton was explicit is not an
established live finalizer deadline and must not be hard-coded as an overdue
threshold anywhere in this module."""


@dataclass(frozen=True)
class SourceReviewQueueSloSnapshot:
    """One observability read over the ordinary source-review queue.

    All age fields are seconds. Percentile/oldest fields are ``None`` only
    when the actionable backlog is empty (no item to measure), never as a
    stand-in for zero. ``overdue_count`` and ``p95_exceeds_threshold`` are
    ``None`` whenever their governing threshold is unset -- an unset
    threshold must never render as "0 overdue" or "healthy".
    """

    backlog_count: int
    active_work_count: int
    capacity_wait_count: int
    infrastructure_backoff_count: int
    escalation_count: int
    p50_age_seconds: float | None
    p95_age_seconds: float | None
    oldest_age_seconds: float | None
    throughput_window_hours: int
    throughput_completed_count: int
    throughput_per_hour: float
    # Reconciliation counts: visible, never folded into the counts above.
    # Cross-link: ditto-subnet#2038 owns transactional cleanup so a
    # screening-attempt row and its agent's status can no longer drift apart
    # in production; until that lands, these counts are how an operator sees
    # the drift instead of it silently inflating (or silently vanishing from)
    # the actionable backlog. See
    # ``test_ghost_rows_disagree_with_repaired_queue_until_2038`` for the
    # regression this module commits to once #2038 lands.
    stale_running_ghost_count: int
    resolved_quarantine_ghost_count: int
    # A latest-attempt status this module's reason CASE does not cover (e.g.
    # a terminal 'passed'/'rejected' verdict recorded against an agent whose
    # own status never advanced past screening) -- the same kind of
    # screening_attempts/agents drift #2038 owns, caught here so it is never
    # silently folded into backlog_count with no reason bucket to show for
    # it. See the module docstring.
    attempt_status_drift_ghost_count: int
    # Configured, observability-only thresholds (never enforced by this
    # module) and their derived, possibly-null verdicts.
    max_actionable_age_threshold_seconds: int | None
    overdue_count: int | None
    p95_age_threshold_seconds: int | None
    p95_exceeds_threshold: bool | None

    @property
    def ghost_count(self) -> int:
        return (
            self.stale_running_ghost_count
            + self.resolved_quarantine_ghost_count
            + self.attempt_status_drift_ghost_count
        )


def _sql_status_list(statuses: tuple[AgentStatus, ...]) -> str:
    """Render an ``AgentStatus`` tuple as a SQL ``IN (...)`` literal list.

    Values come only from the closed ``AgentStatus`` enum, never user input,
    so this is a safe way to keep the query's literals derived from the same
    constants the rest of this module reasons about instead of a hand-copied
    second list that can drift.
    """
    return ", ".join(f"'{status.value}'" for status in statuses)


_SNAPSHOT_SQL = f"""
WITH latest_attempt AS (
    SELECT DISTINCT ON (agent_id)
           agent_id, status AS attempt_status, started_at
      FROM screening_attempts
     ORDER BY agent_id, started_at DESC, attempt_id DESC
),
active_quarantine AS (
    SELECT DISTINCT agent_id FROM screening_quarantines WHERE status = 'active'
),
ordinary_candidates AS (
    SELECT a.agent_id,
           a.created_at,
           la.attempt_status,
           la.started_at AS attempt_started_at,
           aq.agent_id IS NOT NULL AS has_active_quarantine
      FROM agents a
      LEFT JOIN latest_attempt la ON la.agent_id = a.agent_id
      LEFT JOIN active_quarantine aq ON aq.agent_id = a.agent_id
     WHERE a.status IN ({_sql_status_list(ORDINARY_REVIEW_ACTIONABLE_STATUSES)})
       -- A quarantined agent with no active quarantine row is a resolved-quarantine
       -- reconciliation gap (Peyton's item 2), not actionable escalation.
       AND NOT (a.status = 'quarantined' AND aq.agent_id IS NULL)
),
classified AS (
    SELECT agent_id,
           -- An active quarantine wins regardless of what the latest attempt's
           -- own status says (matches load_agent_ordinary_review_state and its
           -- public.py mirror); only once that is ruled out does attempt status
           -- decide. Any attempt status this leaves uncovered (a terminal
           -- 'passed'/'rejected' verdict recorded against an agent whose own
           -- status never advanced) is NULL here on purpose -- see below.
           CASE
             WHEN has_active_quarantine THEN 'escalation'
             WHEN attempt_status IS NULL THEN 'capacity_wait'
             WHEN attempt_status = 'running' THEN 'active_work'
             WHEN attempt_status IN ('failed', 'expired') THEN 'infrastructure_backoff'
             ELSE NULL
           END AS reason,
           -- The stable queue-entry clock: an agent's created_at never moves,
           -- so a retry (a new screening_attempts row) cannot reset it. See
           -- the module docstring.
           extract(epoch FROM now() - created_at) AS age_seconds,
           CASE WHEN attempt_started_at IS NOT NULL
                THEN extract(epoch FROM now() - attempt_started_at)
           END AS current_attempt_age_seconds
      FROM ordinary_candidates
),
actionable AS (
    SELECT agent_id, reason, age_seconds, current_attempt_age_seconds
      FROM classified
     WHERE reason IS NOT NULL
),
ghosts_stale_running AS (
    SELECT a.agent_id
      FROM agents a
      JOIN latest_attempt la ON la.agent_id = a.agent_id
     WHERE la.attempt_status = 'running'
       AND a.status IN ({_sql_status_list(_GHOST_AGENT_STATUSES)})
),
ghosts_resolved_quarantine AS (
    SELECT a.agent_id
      FROM agents a
      LEFT JOIN active_quarantine aq ON aq.agent_id = a.agent_id
     WHERE a.status = 'quarantined' AND aq.agent_id IS NULL
),
ghosts_attempt_status_drift AS (
    SELECT agent_id FROM classified WHERE reason IS NULL
),
throughput AS (
    SELECT count(*)::bigint AS completed
      FROM screening_attempts
     WHERE build_only = false
       AND status IN ('passed', 'rejected')
       AND finished_at IS NOT NULL
       AND finished_at >= now() - make_interval(hours => :throughput_window_hours)
)
SELECT
  (SELECT count(*) FROM actionable)::bigint AS backlog_count,
  (SELECT count(*) FROM actionable WHERE reason = 'active_work')::bigint
      AS active_work_count,
  (SELECT count(*) FROM actionable WHERE reason = 'capacity_wait')::bigint
      AS capacity_wait_count,
  (SELECT count(*) FROM actionable WHERE reason = 'infrastructure_backoff')::bigint
      AS infrastructure_backoff_count,
  (SELECT count(*) FROM actionable WHERE reason = 'escalation')::bigint
      AS escalation_count,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY age_seconds) FROM actionable)
      AS p50_age_seconds,
  (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY age_seconds) FROM actionable)
      AS p95_age_seconds,
  (SELECT max(age_seconds) FROM actionable) AS oldest_age_seconds,
  (SELECT count(*) FROM ghosts_stale_running)::bigint AS stale_running_ghost_count,
  (SELECT count(*) FROM ghosts_resolved_quarantine)::bigint
      AS resolved_quarantine_ghost_count,
  (SELECT count(*) FROM ghosts_attempt_status_drift)::bigint
      AS attempt_status_drift_ghost_count,
  (SELECT completed FROM throughput) AS throughput_completed_count,
  CASE
    WHEN CAST(:max_age_threshold_seconds AS double precision) IS NULL THEN NULL
    ELSE (
      SELECT count(*) FROM actionable
       WHERE age_seconds > CAST(:max_age_threshold_seconds AS double precision)
    )
  END AS overdue_count
"""


async def load_source_review_queue_slo_snapshot(
    session: AsyncSession,
    *,
    throughput_window_hours: int = DEFAULT_THROUGHPUT_WINDOW_HOURS,
    max_actionable_age_threshold_seconds: int | None = None,
    p95_age_threshold_seconds: int | None = None,
) -> SourceReviewQueueSloSnapshot:
    """One bounded read over the ordinary-review queue's current state.

    Thresholds are pure inputs: this module never invents or defaults a
    non-null threshold, and never enforces one (no alert, no escalation
    action -- both are explicit #2042 follow-ups). Passing ``None`` for
    either threshold reports its dependent field(s) as ``None``.
    """
    row = (
        (
            await session.execute(
                text(_SNAPSHOT_SQL),
                {
                    "throughput_window_hours": throughput_window_hours,
                    "max_age_threshold_seconds": max_actionable_age_threshold_seconds,
                },
            )
        )
        .mappings()
        .one()
    )
    p95_age_seconds = (
        float(row["p95_age_seconds"]) if row["p95_age_seconds"] is not None else None
    )
    p95_exceeds_threshold = (
        None
        if p95_age_threshold_seconds is None or p95_age_seconds is None
        else p95_age_seconds > p95_age_threshold_seconds
    )
    throughput_completed_count = int(row["throughput_completed_count"] or 0)
    return SourceReviewQueueSloSnapshot(
        backlog_count=int(row["backlog_count"]),
        active_work_count=int(row["active_work_count"]),
        capacity_wait_count=int(row["capacity_wait_count"]),
        infrastructure_backoff_count=int(row["infrastructure_backoff_count"]),
        escalation_count=int(row["escalation_count"]),
        p50_age_seconds=(
            float(row["p50_age_seconds"])
            if row["p50_age_seconds"] is not None
            else None
        ),
        p95_age_seconds=p95_age_seconds,
        oldest_age_seconds=(
            float(row["oldest_age_seconds"])
            if row["oldest_age_seconds"] is not None
            else None
        ),
        throughput_window_hours=throughput_window_hours,
        throughput_completed_count=throughput_completed_count,
        throughput_per_hour=throughput_completed_count / throughput_window_hours,
        stale_running_ghost_count=int(row["stale_running_ghost_count"]),
        resolved_quarantine_ghost_count=int(row["resolved_quarantine_ghost_count"]),
        attempt_status_drift_ghost_count=int(row["attempt_status_drift_ghost_count"]),
        max_actionable_age_threshold_seconds=max_actionable_age_threshold_seconds,
        overdue_count=(
            int(row["overdue_count"]) if row["overdue_count"] is not None else None
        ),
        p95_age_threshold_seconds=p95_age_threshold_seconds,
        p95_exceeds_threshold=p95_exceeds_threshold,
    )


@dataclass(frozen=True)
class AgentOrdinaryReviewState:
    """One agent's own ordinary-review clock, for the public projection.

    Carries no operator-only detail: no reason codes, no quarantine
    evidence, no cross-agent data. ``reason`` is one of the same four
    public-safe words used in the admin snapshot. ``age_seconds`` is the
    stable queue-entry clock (the agent's own ``created_at``); it does not
    reset on a retry. ``current_attempt_age_seconds`` is the separate,
    optional answer to "how long has the CURRENT attempt been going" -- null
    when there is no attempt yet (``capacity_wait``).
    """

    reason: OrdinaryReviewReason
    age_seconds: float
    current_attempt_age_seconds: float | None = None


async def load_agent_ordinary_review_state(
    session: AsyncSession, *, agent_id: UUID
) -> AgentOrdinaryReviewState | None:
    """The current agent's own ordinary-review state, or ``None``.

    ``None`` covers every agent outside :data:`ORDINARY_REVIEW_ACTIONABLE_STATUSES`
    (never scored yet vs. long since resolved -- both look the same to a
    miner asking "am I in ordinary review right now?") and the
    resolved-quarantine reconciliation gap (a ghost is not shown to a miner
    as an active review any more than it is counted for an operator).

    NOT called by ``endpoints/public.py``'s ``agent_pipeline``: that handler
    reimplements this same reason/clock classification inline, reusing the
    ``attempts``/``quarantines`` rows it already fetched for other fields in
    the same request rather than paying a second round trip. The two are
    behaviourally equivalent by construction; keep them in sync by hand if
    either changes (or fold the inline copy into a call to this function).
    """
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.status not in ORDINARY_REVIEW_ACTIONABLE_STATUSES:
        return None
    latest_attempt = await session.scalar(
        select(ScreeningAttempt)
        .where(ScreeningAttempt.agent_id == agent_id)
        .order_by(
            ScreeningAttempt.started_at.desc(),
            ScreeningAttempt.attempt_id.desc(),
        )
        .limit(1)
    )
    now = datetime.now(UTC)
    if agent.status == AgentStatus.QUARANTINED:
        has_active_quarantine = (
            await session.scalar(
                select(ScreeningQuarantine.quarantine_id).where(
                    ScreeningQuarantine.agent_id == agent_id,
                    ScreeningQuarantine.status == "active",
                )
            )
            is not None
        )
        if not has_active_quarantine:
            return None
        reason: OrdinaryReviewReason = "escalation"
    elif latest_attempt is None:
        reason = "capacity_wait"
    elif latest_attempt.status == "running":
        reason = "active_work"
    elif latest_attempt.status in ("failed", "expired"):
        reason = "infrastructure_backoff"
    else:
        # Defensive: an agent in an actionable status whose latest attempt
        # is itself terminal ('passed'/'rejected'/'quarantined' without a
        # matching Agent.status) is the same kind of drift the ghost counts
        # above track. Do not surface a guess to a miner.
        return None
    # Stable clock: the agent's own created_at, never a retry's started_at.
    age_seconds = max(0.0, (now - agent.created_at).total_seconds())
    current_attempt_age_seconds = (
        max(0.0, (now - latest_attempt.started_at).total_seconds())
        if latest_attempt is not None
        else None
    )
    return AgentOrdinaryReviewState(
        reason=reason,
        age_seconds=age_seconds,
        current_attempt_age_seconds=current_attempt_age_seconds,
    )

"""Bounded confirmation-lane failure-taxonomy reads for Backroom.

``inference_failure_taxonomy`` answers "which model, gateway, route, error code"
for the ordinary chat/embedding lanes. It reads ``inference_requests`` and
cross-joins a fixed ``('chat'), ('embedding')`` lane list, so the confirmation
lane -- which writes to ``confirmation_inference_requests`` -- is not
under-sampled by it, it is absent from it entirely.

That gap was read live on 2026-10-21/22: LongMem confirmation had lost 63 of 79
terminal bundles (83%), every one to a provider rejection, while
``get_inference_failure_taxonomy`` reported ``rate_limited_failures: 0`` and
``rate_limit_bursts.active: false`` in every window. An operator reading the
tool built to surface exactly that failure saw a healthy lane. This module
answers the same question for the confirmation grants.

Two asymmetries are deliberate and load-bearing:

* **Windows are longer.** The ordinary lanes run tens of thousands of calls an
  hour, so 1/5/15/60 minutes is the right grain. A confirmation bundle runs a
  few times a day, so every one of those windows is empty almost always. These
  windows are hours and days instead, which is only affordable because the
  confirmation tables are small -- do not copy them back onto
  ``inference_requests``.
* **The failure detail is the relay's, not inferred.** Since revision
  ``d09a6cba3154`` the relay records, on each failed call, a bounded
  ``failure_kind``, the final ``upstream_http_status``, its
  ``upstream_attempts``, and whether the failure was provably
  ``receipt_free``. Groups are keyed on those, so a 5xx, a timeout and a
  connection reset are separate rows instead of one "failed, no provider, no
  cost" line. That separation is what decides whether a retry is safe (only a
  receipt-free failure can be re-dispatched without double-charging) and how
  it must back off. Before that revision all four are NULL, so a window that
  spans the deploy reports older failures as ``failure_kind = NULL``: they are
  unexplained, not healthy.

Cost rules are inherited from ``inference_failure_taxonomy``: every statement is
bounded by window, projects only the columns it aggregates, and returns a capped
number of rows. Re-EXPLAIN on production
(``.agents/skills/gcloud-ditto-readonly``) before widening the window set.
"""

# ruff: noqa: E501 -- SQL is kept vertically aligned with its result columns.

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

CONFIRMATION_FAILURE_WINDOWS_SECONDS = (3600, 86400, 604800)
"""1 hour / 1 day / 7 days. A bundle runs a few times a day, so the ordinary
60/300/900/3600 grain reports an empty lane almost always."""

CONFIRMATION_FAILURE_GROUP_LIMIT = 40
"""Groups returned per (window, lane); real cardinality sits far below it."""

# ``upstream_provider`` on a confirmation request means the same thing it means
# on an ordinary one: the single ``selected`` endpoint the relay parsed out of a
# SUCCESSFUL response. On a failure it is NULL whenever no endpoint was ever
# selected, which is precisely the pre-provider rejection shape. Provider names
# arrive inside an untrusted provider response, so they are bounded here exactly
# as the ordinary taxonomy bounds them and never reach an operator as free text.
_SAFE_PROVIDER_RE = "^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,119}$"

_WINDOWS_SQL = """
    SELECT unnest(CAST(:windows AS int[])) AS window_seconds
"""

_RECENT_SQL = f"""
    SELECT g.lane                                       AS lane,
           g.bundle_id                                  AS bundle_id,
           left(r.model, 120)                           AS model,
           CASE
             WHEN r.upstream_provider IS NULL THEN NULL
             WHEN r.upstream_provider ~ '{_SAFE_PROVIDER_RE}' THEN r.upstream_provider
             ELSE NULL
           END                                          AS upstream_provider,
           CASE
             WHEN r.upstream_provider IS NULL THEN 'unknown'
             WHEN r.upstream_provider ~ '{_SAFE_PROVIDER_RE}' THEN 'confirmed_selected'
             ELSE 'unrecognized'
           END                                          AS provider_basis,
           r.status                                     AS status,
           r.failure_kind                               AS failure_kind,
           r.upstream_http_status                       AS upstream_http_status,
           r.upstream_attempts                          AS upstream_attempts,
           r.receipt_free                               AS receipt_free,
           r.cost_microusd                              AS cost_microusd,
           r.started_at                                 AS started_at,
           r.completed_at                               AS completed_at,
           CASE
             WHEN r.completed_at IS NULL THEN NULL
             ELSE CAST(EXTRACT(EPOCH FROM (r.completed_at - r.started_at)) * 1000 AS bigint)
           END                                          AS duration_ms
      FROM confirmation_inference_requests r
      JOIN confirmation_inference_grants g ON g.grant_id = r.grant_id
     WHERE r.started_at >= now() - make_interval(secs => :max_window)
"""

CONFIRMATION_LANE_TOTALS_SQL = f"""
WITH windows AS ({_WINDOWS_SQL}),
recent AS ({_RECENT_SQL})
  SELECT w.window_seconds                                                        AS window_seconds,
         lane.lane                                                               AS lane,
         count(r.lane)::bigint                                                   AS calls,
         count(*) FILTER (WHERE r.status IN ('completed', 'failed', 'canceled')) AS settled,
         count(*) FILTER (WHERE r.status = 'completed')                          AS completed,
         count(*) FILTER (WHERE r.status = 'failed')                             AS failed,
         count(*) FILTER (WHERE r.status = 'started')                            AS in_flight,
         count(*) FILTER (WHERE r.status = 'failed' AND r.upstream_provider IS NULL)   AS failed_without_provider,
         count(*) FILTER (WHERE r.status = 'failed' AND r.cost_microusd = 0)      AS failed_without_cost,
         count(DISTINCT r.bundle_id) FILTER (WHERE r.status = 'failed')::bigint   AS bundles_with_failure,
         COALESCE(max(r.duration_ms) FILTER (WHERE r.status = 'failed'), 0)       AS failed_max_duration_ms,
         COALESCE(CAST(percentile_disc(0.5) WITHIN GROUP (ORDER BY r.duration_ms)
                       FILTER (WHERE r.status = 'failed') AS bigint), 0)          AS failed_p50_duration_ms
    FROM windows w
    CROSS JOIN (VALUES ('reader'), ('judge'), ('embedding')) AS lane(lane)
    LEFT JOIN recent r
      ON r.lane = lane.lane
     AND r.started_at >= now() - make_interval(secs => w.window_seconds)
   GROUP BY w.window_seconds, lane.lane
   ORDER BY w.window_seconds, lane.lane
"""

CONFIRMATION_FAILURE_GROUPS_SQL = f"""
WITH windows AS ({_WINDOWS_SQL}),
recent AS ({_RECENT_SQL}),
grouped AS (
  SELECT w.window_seconds,
         r.lane,
         r.model,
         r.upstream_provider,
         r.provider_basis,
         r.status,
         r.failure_kind,
         r.upstream_http_status,
         r.receipt_free,
         count(*)                                                        AS calls,
         COALESCE(max(r.upstream_attempts), 0)                           AS max_upstream_attempts,
         count(*) FILTER (WHERE r.cost_microusd = 0)                     AS without_cost,
         count(DISTINCT r.bundle_id)                                     AS bundles_affected,
         COALESCE(max(r.duration_ms), 0)                                 AS max_duration_ms,
         COALESCE(CAST(percentile_disc(0.5) WITHIN GROUP (ORDER BY r.duration_ms) AS bigint), 0) AS p50_duration_ms
    FROM windows w
    JOIN recent r
      ON r.started_at >= now() - make_interval(secs => w.window_seconds)
   GROUP BY w.window_seconds, r.lane, r.model, r.upstream_provider, r.provider_basis, r.status,
            r.failure_kind, r.upstream_http_status, r.receipt_free
),
ranked AS (
  SELECT grouped.*,
         row_number() OVER (
           PARTITION BY window_seconds, lane
           ORDER BY calls DESC, model ASC, status ASC,
                    failure_kind ASC NULLS FIRST, upstream_http_status ASC NULLS FIRST
         ) AS rank_in_lane
    FROM grouped
)
SELECT * FROM ranked
 WHERE rank_in_lane <= :group_limit
 ORDER BY window_seconds, lane, calls DESC, model
"""


async def load_confirmation_failure_taxonomy_rows(
    session: AsyncSession,
    *,
    group_limit: int = CONFIRMATION_FAILURE_GROUP_LIMIT,
    windows: Sequence[int] = CONFIRMATION_FAILURE_WINDOWS_SECONDS,
) -> tuple[Sequence[RowMapping], Sequence[RowMapping]]:
    """Return per-window confirmation lane totals and capped per-window groups."""
    params = {"windows": list(windows), "max_window": max(windows)}
    lanes = (
        (await session.execute(text(CONFIRMATION_LANE_TOTALS_SQL), params))
        .mappings()
        .all()
    )
    groups = (
        (
            await session.execute(
                text(CONFIRMATION_FAILURE_GROUPS_SQL),
                {**params, "group_limit": group_limit},
            )
        )
        .mappings()
        .all()
    )
    return lanes, groups

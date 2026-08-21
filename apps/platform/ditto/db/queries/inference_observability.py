"""Bounded aggregate reads for hosted-inference runtime diagnostics."""

# ruff: noqa: E501 -- SQL is kept vertically aligned with its result columns.

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

WINDOWS_SECONDS = (60, 300, 900, 3600)

# Every read here is priced against the live ledger, not a fixture. On
# 2026-08-21 ``inference_requests`` held 15 million rows (5 GB) and turned over
# ~80k rows an hour, and this module's three statements cost: the stale count
# >120 s (it walked the whole ledger), windows 6.5 s, peaks 4.4 s -- against a
# 30 s Backroom budget. Three rules keep them cheap:
#
# * The stale count has no time bound on purpose -- it must see every stuck
#   request -- so it rides ``inference_requests_inflight_idx``, a partial index
#   over ``status = 'started'`` rows only. That is a few dozen entries whatever
#   the ledger holds.
# * The hour-bounded sweeps project only the columns they aggregate. ``SELECT
#   r.*`` materialised 195-byte rows and every sort spilled at an 8 MB
#   ``work_mem``; the narrow rows sort in memory.
# * Concurrency peaks are one ordered sweep per partition key. The window
#   cross-join that replayed the hour four times (once per window) is now one
#   running sum with a ``FILTER`` per window, and the per-owner sweep keys on
#   a small integer rank instead of a 48-byte hotkey.
#
# Measured on that ledger after the rewrite: windows 1.0 s, peaks 1.3 s, and
# the stale count an index walk. Keep it that way: re-EXPLAIN on production
# (``.agents/skills/gcloud-ditto-readonly``) before widening any of these.

CURRENT_SQL = """
SELECT lane.request_kind,
       CASE lane.request_kind
         WHEN 'chat' THEN COALESCE(sum(g.active_requests), 0)
         ELSE COALESCE(sum(g.embedding_active_requests), 0)
       END::bigint AS active_requests,
       count(g.grant_id)::bigint AS live_grants,
       (SELECT count(*)
          FROM inference_requests r
         WHERE r.request_kind = lane.request_kind
           AND r.status = 'started'
           AND r.started_at < now() - make_interval(secs => :stale_after_seconds)
       )::bigint AS stale_started_requests
  FROM (VALUES ('chat'), ('embedding')) AS lane(request_kind)
 LEFT JOIN inference_grants g
        ON g.status = 'active' AND g.expires_at > now()
 GROUP BY lane.request_kind
 ORDER BY lane.request_kind
"""

WINDOWS_SQL = """
WITH recent AS (
    SELECT r.request_kind,
           r.started_at,
           COALESCE(r.completed_at, now()) AS ended_at,
           r.status,
           r.timed_out,
           r.latency_ms,
           r.prompt_tokens + r.completion_tokens AS tokens
      FROM inference_requests r
     WHERE r.started_at >= now() - interval '60 minutes'
),
windows(window_seconds) AS (
    SELECT unnest(ARRAY[60, 300, 900, 3600]::integer[])
),
aggregates AS (
    SELECT w.window_seconds,
           r.request_kind,
           count(*)::bigint AS calls,
           count(*)::double precision / w.window_seconds AS calls_per_second,
           COALESCE(sum(r.tokens), 0)::bigint AS tokens,
           COALESCE(sum(r.tokens), 0)::double precision / w.window_seconds AS tokens_per_second,
           count(*) FILTER (WHERE r.status = 'completed')::bigint AS completed,
           count(*) FILTER (WHERE r.status = 'failed')::bigint AS failed,
           count(*) FILTER (WHERE r.status = 'canceled')::bigint AS canceled,
           count(*) FILTER (WHERE r.timed_out)::bigint AS timed_out,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY r.latency_ms)
               FILTER (WHERE r.latency_ms IS NOT NULL) AS latency_p50_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY r.latency_ms)
               FILTER (WHERE r.latency_ms IS NOT NULL) AS latency_p95_ms,
           max(r.latency_ms) AS latency_max_ms
      FROM windows w
      JOIN recent r
        ON r.started_at >= now() - make_interval(secs => w.window_seconds)
  GROUP BY w.window_seconds, r.request_kind
),
events AS (
    SELECT request_kind, started_at, started_at AS at, 1 AS delta FROM recent
    UNION ALL
    SELECT request_kind, started_at, ended_at AS at, -1 AS delta FROM recent
),
running AS (
    -- One sweep in time order per lane. A window's running sum only counts
    -- the events of requests that started inside it; on the rows it skips it
    -- repeats the last value, so its maximum is the same peak the per-window
    -- sweep produced. The default frame nets events that share a timestamp.
    SELECT request_kind,
           sum(delta) FILTER (WHERE started_at >= now() - interval '60 seconds') OVER lane AS active_60,
           sum(delta) FILTER (WHERE started_at >= now() - interval '300 seconds') OVER lane AS active_300,
           sum(delta) FILTER (WHERE started_at >= now() - interval '900 seconds') OVER lane AS active_900,
           sum(delta) OVER lane AS active_3600
      FROM events
    WINDOW lane AS (PARTITION BY request_kind ORDER BY at)
),
peaks AS (
    SELECT request_kind,
           max(active_60) AS peak_60,
           max(active_300) AS peak_300,
           max(active_900) AS peak_900,
           max(active_3600) AS peak_3600
      FROM running
  GROUP BY request_kind
),
peak_rows AS (
    SELECT p.request_kind, pw.window_seconds, pw.peak_global_concurrency
      FROM peaks p
     CROSS JOIN LATERAL (
           VALUES (60, p.peak_60), (300, p.peak_300), (900, p.peak_900), (3600, p.peak_3600)
     ) AS pw(window_seconds, peak_global_concurrency)
)
SELECT a.*, p.peak_global_concurrency::bigint AS peak_global_concurrency
  FROM aggregates a
  JOIN peak_rows p USING (window_seconds, request_kind)
 ORDER BY a.window_seconds, a.request_kind
"""

PEAKS_SQL = """
WITH recent AS (
    SELECT r.grant_id,
           r.request_kind,
           r.started_at,
           COALESCE(r.completed_at, now()) AS ended_at
      FROM inference_requests r
     WHERE r.started_at >= now() - interval '60 minutes'
),
owners AS (
    -- The validator partition key as a small integer: ranking the few dozen
    -- grants active in an hour is free, and it keeps every event row narrow
    -- enough that both sweeps below sort in memory.
    SELECT g.grant_id,
           dense_rank() OVER (ORDER BY g.validator_hotkey)::integer AS validator_no
      FROM inference_grants g
     WHERE g.grant_id IN (SELECT DISTINCT grant_id FROM recent)
),
events AS (
    SELECT r.grant_id, o.validator_no, r.request_kind, r.started_at AS at, 1 AS delta
      FROM recent r JOIN owners o USING (grant_id)
    UNION ALL
    SELECT r.grant_id, o.validator_no, r.request_kind, r.ended_at AS at, -1 AS delta
      FROM recent r JOIN owners o USING (grant_id)
),
running AS (
    SELECT request_kind,
           sum(delta) OVER (PARTITION BY grant_id, request_kind ORDER BY at) AS ticket_active,
           sum(delta) OVER (PARTITION BY validator_no, request_kind ORDER BY at) AS validator_active
      FROM events
),
peaks AS (
    SELECT request_kind,
           max(ticket_active) AS ticket_peak,
           max(validator_active) AS validator_peak
      FROM running
  GROUP BY request_kind
)
SELECT s.scope, p.request_kind, s.peak::bigint AS peak
  FROM peaks p
 CROSS JOIN LATERAL (
       VALUES ('ticket'::text, p.ticket_peak), ('validator'::text, p.validator_peak)
 ) AS s(scope, peak)
 ORDER BY s.scope, p.request_kind
"""


async def load_inference_runtime_rows(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
) -> tuple[Sequence[RowMapping], Sequence[RowMapping], Sequence[RowMapping]]:
    """Return current load, recent windows, and 60-minute peak rows.

    The hour-bounded sweeps ride ``inference_requests_started_idx``; the
    unbounded stale count rides the partial ``inference_requests_inflight_idx``.
    Nothing here reconstructs concurrency from table birth.
    """
    current = (
        (
            await session.execute(
                text(CURRENT_SQL), {"stale_after_seconds": stale_after_seconds}
            )
        )
        .mappings()
        .all()
    )
    windows = (await session.execute(text(WINDOWS_SQL))).mappings().all()
    peaks = (await session.execute(text(PEAKS_SQL))).mappings().all()
    return current, windows, peaks

"""Bounded aggregate reads for hosted-inference runtime diagnostics."""

# ruff: noqa: E501 -- SQL is kept vertically aligned with its result columns.

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

WINDOWS_SECONDS = (60, 300, 900, 3600)


async def load_inference_runtime_rows(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
) -> tuple[Sequence[RowMapping], Sequence[RowMapping], Sequence[RowMapping]]:
    """Return current load, recent windows, and 60-minute peak rows.

    The request ledger already has indexes on ``started_at`` and
    ``(request_kind, started_at)``. Every event sweep is therefore bounded to
    the most recent hour; it never reconstructs concurrency from table birth.
    """
    current = (
        (
            await session.execute(
                text(
                    """
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
                ),
                {"stale_after_seconds": stale_after_seconds},
            )
        )
        .mappings()
        .all()
    )

    windows = (
        (
            await session.execute(
                text(
                    """
                WITH windows(window_seconds) AS (
                    SELECT unnest(ARRAY[60, 300, 900, 3600]::integer[])
                ),
                recent AS (
                    SELECT r.*
                      FROM inference_requests r
                     WHERE r.started_at >= now() - interval '60 minutes'
                ),
                aggregates AS (
                    SELECT w.window_seconds,
                           r.request_kind,
                           count(*)::bigint AS calls,
                           count(*)::double precision / w.window_seconds AS calls_per_second,
                           COALESCE(sum(r.prompt_tokens + r.completion_tokens), 0)::bigint AS tokens,
                           COALESCE(sum(r.prompt_tokens + r.completion_tokens), 0)::double precision / w.window_seconds AS tokens_per_second,
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
                    SELECT w.window_seconds, r.request_kind, r.started_at AS at, 1 AS delta
                      FROM windows w
                      JOIN recent r
                        ON r.started_at >= now() - make_interval(secs => w.window_seconds)
                    UNION ALL
                    SELECT w.window_seconds, r.request_kind,
                           COALESCE(r.completed_at, now()) AS at, -1 AS delta
                      FROM windows w
                      JOIN recent r
                        ON r.started_at >= now() - make_interval(secs => w.window_seconds)
                ),
                running AS (
                    SELECT window_seconds, request_kind,
                           sum(sum(delta)) OVER (
                               PARTITION BY window_seconds, request_kind ORDER BY at
                           ) AS active
                      FROM events
                  GROUP BY window_seconds, request_kind, at
                ),
                peaks AS (
                    SELECT window_seconds, request_kind, max(active)::bigint AS peak_global_concurrency
                      FROM running
                  GROUP BY window_seconds, request_kind
                )
                SELECT a.*, p.peak_global_concurrency
                  FROM aggregates a
                  JOIN peaks p USING (window_seconds, request_kind)
              ORDER BY a.window_seconds, a.request_kind
                """
                )
            )
        )
        .mappings()
        .all()
    )

    peaks = (
        (
            await session.execute(
                text(
                    """
                WITH recent AS (
                    SELECT r.*, g.validator_hotkey
                      FROM inference_requests r
                      JOIN inference_grants g USING (grant_id)
                     WHERE r.started_at >= now() - interval '60 minutes'
                ),
                owners AS (
                    SELECT 'ticket'::text AS scope, grant_id::text AS owner,
                           request_kind, started_at AS at, 1 AS delta FROM recent
                    UNION ALL
                    SELECT 'ticket', grant_id::text, request_kind,
                           COALESCE(completed_at, now()), -1 FROM recent
                    UNION ALL
                    SELECT 'validator', validator_hotkey, request_kind,
                           started_at, 1 FROM recent
                    UNION ALL
                    SELECT 'validator', validator_hotkey, request_kind,
                           COALESCE(completed_at, now()), -1 FROM recent
                ),
                running AS (
                    SELECT scope, owner, request_kind,
                           sum(sum(delta)) OVER (
                               PARTITION BY scope, owner, request_kind ORDER BY at
                           ) AS active
                      FROM owners
                  GROUP BY scope, owner, request_kind, at
                )
                SELECT scope, request_kind, max(active)::bigint AS peak
                  FROM running
              GROUP BY scope, request_kind
              ORDER BY scope, request_kind
                """
                )
            )
        )
        .mappings()
        .all()
    )
    return current, windows, peaks

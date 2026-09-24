"""Bounded chat/embedding failure-taxonomy reads for Backroom.

The 2026-09-22 shared-pool burst (thousands of ``upstream_http_429`` on one
model for ~22 minutes) was visible in the ledger and invisible to operators:
``/admin/inference-runtime-metrics`` reports failures per lane per window, so it
could say "209 of 903 chat calls failed" and could not say which model, which
gateway, which upstream route, or which error code. This module answers that
question over the same bounded windows and nothing else -- no prompts,
responses, keys, headers, or trace bodies, only counts and identifiers.

Cost rules are inherited from ``inference_observability``: every statement is
bounded to the last 60 minutes so it rides
``inference_requests_kind_started_idx``, projects only the columns it
aggregates, and returns a capped number of rows. Re-EXPLAIN on production
(``.agents/skills/gcloud-ditto-readonly``) before widening either window set.
"""

# ruff: noqa: E501 -- SQL is kept vertically aligned with its result columns.

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

FAILURE_WINDOWS_SECONDS = (60, 300, 900, 3600)
"""1/5/15/60 minutes. The incident was read at 1-minute and 5-minute grain."""

FAILURE_GROUP_LIMIT = 40
"""Groups returned per (window, lane); real cardinality sits far below it."""

# What the ledger can and cannot testify to about a route. ``upstream_provider``
# is one column carrying four different meanings, and conflating them is exactly
# how the burst was misread:
#
# * chat + completed -- the single ``selected`` endpoint parsed out of
#   ``openrouter_metadata.endpoints.available`` by the relay
#   (``upstreamProviderIdentity`` in services/model-relay). A CONFIRMED serving
#   route.
# * chat + failed/canceled -- ``openrouter_metadata.attempts[-1].provider``
#   (``openrouterLastAttemptedProvider``): the LAST ATTEMPTED upstream, kept
#   only when the error envelope carried router metadata. Not a confirmed
#   route, and NULL whenever the provider returned no metadata.
# * embedding, any status -- the relay's own configured ``EmbeddingProvider``
#   constant, stamped onto the outcome before the call is made. Never observed
#   from a response.
# * ``'ditto-router'`` -- the Ditto Router dogfood lane, which selects the
#   upstream itself and does not report it. A route marker, not a provider.
#
# So the basis travels with the value: NULL reports as ``unknown`` with no
# route, and a value that is not a plain bounded identifier reports as
# ``unrecognized`` with no route. Provider names arrive inside an untrusted
# provider response and must never reach an operator surface as free text.
_SAFE_PROVIDER_RE = "^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,119}$"

_CLASSIFIED_SQL = f"""
    SELECT r.request_kind,
           left(r.model, 120) AS model,
           r.started_at,
           r.status,
           r.timed_out,
           r.openrouter_attempts,
           left(r.terminal_error_code, 120) AS terminal_error_code,
           CASE
             WHEN r.upstream_provider = 'ditto-router'             THEN 'ditto-router'
             WHEN r.request_kind = 'chat' AND r.fallback_phase = 0 THEN 'openrouter'
             WHEN r.request_kind = 'chat'                          THEN 'reliable'
             WHEN r.fallback_phase = 0                             THEN 'direct'
             ELSE 'openrouter'
           END AS gateway,
           CASE
             WHEN r.upstream_provider IS NULL OR r.upstream_provider = '' THEN NULL
             WHEN r.upstream_provider = 'ditto-router'                    THEN NULL
             WHEN r.upstream_provider !~ '{_SAFE_PROVIDER_RE}'            THEN NULL
             ELSE r.upstream_provider
           END AS upstream_route,
           CASE
             WHEN r.upstream_provider = 'ditto-router'                    THEN 'router_internal'
             WHEN r.upstream_provider IS NULL OR r.upstream_provider = '' THEN 'unknown'
             WHEN r.upstream_provider !~ '{_SAFE_PROVIDER_RE}'            THEN 'unrecognized'
             WHEN r.request_kind = 'embedding'                            THEN 'configured'
             WHEN r.status = 'completed'                                  THEN 'confirmed_selected'
             ELSE 'last_attempted'
           END AS route_basis,
           CASE
             WHEN r.terminal_error_code ~ '^upstream_http_[1-5][0-9][0-9]$'
             THEN substring(r.terminal_error_code from 15)::integer
           END AS upstream_http_status
      FROM inference_requests r
     WHERE r.started_at >= now() - interval '60 minutes'
       AND r.status IN ('completed', 'failed', 'canceled')
"""

FAILURE_GROUPS_SQL = f"""
WITH classified AS ({_CLASSIFIED_SQL}),
windows(window_seconds) AS (
    SELECT unnest(ARRAY[60, 300, 900, 3600]::integer[])
),
grouped AS (
    SELECT w.window_seconds,
           c.request_kind,
           c.model,
           c.gateway,
           c.upstream_route,
           c.route_basis,
           c.terminal_error_code,
           c.upstream_http_status,
           count(*)::bigint AS calls,
           count(*) FILTER (WHERE c.status = 'completed')::bigint AS completed,
           count(*) FILTER (WHERE c.status = 'failed')::bigint AS failed,
           count(*) FILTER (WHERE c.status = 'canceled')::bigint AS canceled,
           count(*) FILTER (WHERE c.timed_out)::bigint AS timed_out,
           max(c.openrouter_attempts)::integer AS openrouter_attempts_max
      FROM windows w
      JOIN classified c
        ON c.started_at >= now() - make_interval(secs => w.window_seconds)
  GROUP BY w.window_seconds, c.request_kind, c.model, c.gateway, c.upstream_route,
           c.route_basis, c.terminal_error_code, c.upstream_http_status
),
ranked AS (
    -- Worst first, so a truncated list keeps the burst rather than the noise.
    SELECT g.*,
           row_number() OVER (
               PARTITION BY g.window_seconds, g.request_kind
               ORDER BY g.failed DESC, g.calls DESC, g.model, g.gateway,
                        g.upstream_route NULLS LAST,
                        g.terminal_error_code NULLS LAST) AS group_rank,
           count(*) OVER (PARTITION BY g.window_seconds, g.request_kind)::bigint AS groups_total
      FROM grouped g
)
SELECT * FROM ranked
 WHERE group_rank <= :group_limit
 ORDER BY window_seconds, request_kind, group_rank
"""

# Lane totals are computed independently of the groups, so they still count the
# rows a truncated group list dropped and can also report the in-flight rows
# the taxonomy deliberately excludes: a 'started' row has no route and no error
# code yet, and grouping it would manufacture an 'unknown' route. Both lanes are
# emitted for every window so a silent lane reads as zero, not as a missing row.
FAILURE_LANE_TOTALS_SQL = """
WITH recent AS (
    SELECT r.request_kind, r.started_at, r.status, r.timed_out, r.terminal_error_code
      FROM inference_requests r
     WHERE r.started_at >= now() - interval '60 minutes'
),
windows(window_seconds) AS (
    SELECT unnest(ARRAY[60, 300, 900, 3600]::integer[])
)
SELECT w.window_seconds,
       lane.request_kind,
       count(r.request_kind)::bigint AS calls,
       count(*) FILTER (WHERE r.status IN ('completed', 'failed', 'canceled'))::bigint AS settled,
       count(*) FILTER (WHERE r.status = 'completed')::bigint AS completed,
       count(*) FILTER (WHERE r.status = 'failed')::bigint AS failed,
       count(*) FILTER (WHERE r.status = 'canceled')::bigint AS canceled,
       count(*) FILTER (WHERE r.status = 'started')::bigint AS in_flight,
       count(*) FILTER (WHERE r.timed_out)::bigint AS timed_out,
       count(*) FILTER (WHERE r.terminal_error_code = 'upstream_http_429')::bigint AS rate_limited_failures
  FROM windows w
 CROSS JOIN (VALUES ('chat'), ('embedding')) AS lane(request_kind)
  LEFT JOIN recent r
         ON r.request_kind = lane.request_kind
        AND r.started_at >= now() - make_interval(secs => w.window_seconds)
 GROUP BY w.window_seconds, lane.request_kind
 ORDER BY w.window_seconds, lane.request_kind
"""


async def load_inference_failure_taxonomy_rows(
    session: AsyncSession,
    *,
    group_limit: int = FAILURE_GROUP_LIMIT,
) -> tuple[Sequence[RowMapping], Sequence[RowMapping]]:
    """Return per-window lane totals and the capped per-window group rows."""
    lanes = (await session.execute(text(FAILURE_LANE_TOTALS_SQL))).mappings().all()
    groups = (
        (await session.execute(text(FAILURE_GROUPS_SQL), {"group_limit": group_limit}))
        .mappings()
        .all()
    )
    return lanes, groups

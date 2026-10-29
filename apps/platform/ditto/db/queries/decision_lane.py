"""Bounded aggregate reads for the dark decision-model lane.

Every statement rides ``inference_requests_kind_started_idx`` (request_kind,
started_at) or ``inference_grants_expiry_idx``, so the cost tracks decision
traffic, not the size of the chat ledger.
"""

# ruff: noqa: E501 -- SQL is kept vertically aligned with its result columns.

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

WINDOWS_SECONDS = (3600, 86_400, 604_800)

_USAGE_SQL = """
WITH windows(window_seconds) AS (SELECT unnest(CAST(:windows AS integer[]))),
recent AS (
    SELECT r.started_at, r.status, r.prompt_tokens, r.cost_microusd,
           COALESCE(r.terminal_error_code, '') AS code
      FROM inference_requests r
     WHERE r.request_kind = 'decision'
       AND r.started_at >= now() - make_interval(secs => :max_window)
)
SELECT w.window_seconds,
       count(r.started_at)::bigint                                              AS requests,
       count(*) FILTER (WHERE r.status = 'completed')::bigint                   AS completed,
       count(*) FILTER (WHERE r.status = 'failed')::bigint                      AS failed,
       count(*) FILTER (WHERE r.status = 'canceled')::bigint                    AS canceled,
       COALESCE(sum(r.prompt_tokens), 0)::bigint                                AS input_tokens,
       COALESCE(sum(r.cost_microusd), 0)::bigint                                AS ledger_cost_microusd,
       count(*) FILTER (WHERE r.code = 'decision_model_mismatch')::bigint       AS model_mismatches,
       count(*) FILTER (WHERE r.code LIKE 'decision_provider_rejected_%')::bigint AS provider_rejections,
       count(*) FILTER (WHERE r.status = 'failed' AND r.code <> 'decision_model_mismatch'
                         AND r.code NOT LIKE 'decision_provider_rejected_%')::bigint AS provider_failures,
       (SELECT count(*) FROM inference_grants g
         WHERE g.decision_model IS NOT NULL
           AND g.expires_at >= now() - make_interval(secs => w.window_seconds))::bigint AS grants_with_lane
  FROM windows w
  LEFT JOIN recent r ON r.started_at >= now() - make_interval(secs => w.window_seconds)
 GROUP BY w.window_seconds
 ORDER BY w.window_seconds
"""

_ERRORS_SQL = """
SELECT r.terminal_error_code, count(*)::bigint AS requests
  FROM inference_requests r
 WHERE r.request_kind = 'decision'
   AND r.started_at >= now() - interval '24 hours'
   AND r.terminal_error_code IS NOT NULL
 GROUP BY r.terminal_error_code
 ORDER BY requests DESC, r.terminal_error_code
 LIMIT 20
"""

_SUMMARY_SQL = """
SELECT (SELECT max(r.started_at) FROM inference_requests r
         WHERE r.request_kind = 'decision') AS last_request_at,
       (SELECT count(*) FROM inference_grants g
         WHERE g.decision_model IS NOT NULL AND g.expires_at > now())::bigint AS live_grants_with_lane
"""


async def load_decision_lane_usage(
    session: AsyncSession,
) -> tuple[list[RowMapping], list[RowMapping], RowMapping]:
    usage = (
        await session.execute(
            text(_USAGE_SQL),
            {"windows": list(WINDOWS_SECONDS), "max_window": max(WINDOWS_SECONDS)},
        )
    ).mappings()
    errors = (await session.execute(text(_ERRORS_SQL))).mappings()
    summary = (await session.execute(text(_SUMMARY_SQL))).mappings().one()
    return list(usage), list(errors), summary

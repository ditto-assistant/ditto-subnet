"""Small grant-ledger scan; never classify a historical lease by a reused ticket."""

# ruff: noqa: E501 -- keep SQL projection and predicates readable.

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

COSTS_SQL = """
WITH classified AS (
 SELECT g.bench_version, g.request_count, g.prompt_tokens, g.completion_tokens,
   g.cost_microusd, g.embedding_request_count, g.embedding_tokens,
   g.embedding_cost_microusd,
   CASE
    WHEN EXISTS (SELECT 1 FROM generator_round_attempts a
      JOIN generator_rounds r ON r.round_index=a.round_index
      WHERE r.bench_version=g.bench_version AND a.agent_id=g.agent_id AND a.validator_hotkey=g.validator_hotkey
        AND a.ticket_deadline=g.ticket_deadline) THEN 'round_attempt'
    WHEN EXISTS (SELECT 1 FROM benchmark_canaries c
      WHERE c.agent_id=g.agent_id AND c.validator_hotkey=g.validator_hotkey
        AND c.bench_version=g.bench_version AND c.deadline=g.ticket_deadline) THEN 'canary_lease'
    WHEN t.deadline=g.ticket_deadline AND t.purpose_revision>0
      AND t.purpose<>'legacy_unclassified' THEN 'current_exact_lease'
    WHEN EXISTS (SELECT 1 FROM score_audit_log a
      WHERE a.agent_id=g.agent_id AND a.validator_hotkey=g.validator_hotkey
        AND a.event='score' AND a.payload->>'bench_version'=g.bench_version::text
        AND (a.payload->>'ticket_deadline'=
          to_char(g.ticket_deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
          OR (g.ticket_deadline=date_trunc('second',g.ticket_deadline)
            AND a.payload->>'ticket_deadline'=
              to_char(g.ticket_deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') || '+00:00')))
      THEN 'accepted_score_receipt'
    ELSE 'unknown'
   END AS classification_basis,
   CASE WHEN t.deadline=g.ticket_deadline AND t.purpose_revision>0
     THEN t.purpose ELSE NULL END AS current_purpose
 FROM inference_grants g
 LEFT JOIN validator_tickets t ON t.agent_id=g.agent_id
   AND t.bench_version=g.bench_version AND t.validator_hotkey=g.validator_hotkey
 WHERE g.created_at>=:window_start AND g.created_at<:window_end
), labeled AS (
 SELECT *,
   CASE classification_basis
    WHEN 'round_attempt' THEN 'generator_round'
    WHEN 'canary_lease' THEN 'benchmark_canary'
    WHEN 'current_exact_lease' THEN current_purpose
    WHEN 'accepted_score_receipt' THEN 'accepted_canonical_score'
    ELSE 'historical_unclassified'
   END AS purpose
 FROM classified
)
SELECT purpose,classification_basis,bench_version,count(*)::bigint AS grants,
 COALESCE(sum(request_count),0)::bigint AS chat_calls,
 COALESCE(sum(prompt_tokens),0)::bigint AS prompt_tokens,
 COALESCE(sum(completion_tokens),0)::bigint AS completion_tokens,
 COALESCE(sum(cost_microusd),0)::bigint AS chat_cost_microusd,
 COALESCE(sum(embedding_request_count),0)::bigint AS embedding_calls,
 COALESCE(sum(embedding_tokens),0)::bigint AS embedding_tokens,
 COALESCE(sum(embedding_cost_microusd),0)::bigint AS embedding_cost_microusd
FROM labeled GROUP BY purpose,classification_basis,bench_version
ORDER BY sum(cost_microusd)+sum(embedding_cost_microusd) DESC,purpose,bench_version
"""


async def load_inference_grant_costs(
    session: AsyncSession, *, window_start: datetime, window_end: datetime
) -> list[RowMapping]:
    rows = await session.execute(
        text(COSTS_SQL),
        {"window_start": window_start, "window_end": window_end},
    )
    return list(rows.mappings())

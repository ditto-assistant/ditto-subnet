"""What dueling season rotation re-tests really cost, from the grant ledger.

A rotation re-test lease is one ``score_retest_requested`` entry (basis
``dueling_season_rotation``) whose ``replacement_deadline`` is the lease the
validator's inference grant is bound to (``inference_grants`` is unique per
``(agent, bench_version, validator, ticket_deadline)``). Its spend is that
grant's metered chat, embedding and decision cost: what the relay booked,
including runs that failed, never an estimate. A lease with no grant (it
ended before the validator fetched credentials) is counted as a run at zero
and reported as unpriced.

Opening rotation and season catch-up (``catch_up`` in the payload) are split
so the catch-up's incremental cost reads next to the opening's.
"""

# ruff: noqa: E501 -- keep SQL projection and predicates readable.

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.audit import EVENT_SCORE_RETEST_REQUESTED
from ditto.db.queries.score_retests import DUELING_ROTATION_RETEST_BASIS

_SPEND_SQL = """
SELECT a.payload->>'dueling_season_id' AS season_id,
       COALESCE((a.payload->>'catch_up')::boolean, false) AS catch_up,
       count(*)::bigint AS runs,
       count(g.grant_id)::bigint AS priced_runs,
       COALESCE(sum(g.cost_microusd + g.embedding_cost_microusd
                    + g.decision_cost_microusd), 0)::bigint AS cost_microusd
FROM score_audit_log a
LEFT JOIN inference_grants g
  ON g.agent_id = a.agent_id
 AND g.validator_hotkey = a.validator_hotkey
 AND g.bench_version = (a.payload->>'bench_version')::int
 AND g.ticket_deadline = (a.payload->>'replacement_deadline')::timestamptz
WHERE a.event = :event
  AND a.payload->>'basis' = :basis
  AND {scope}
GROUP BY 1, 2
"""


@dataclass
class RotationSpend:
    """Leases started and their metered cost."""

    runs: int = 0
    priced_runs: int = 0
    cost_microusd: int = 0

    @property
    def cost_usd(self) -> float:
        return round(self.cost_microusd / 1_000_000, 6)

    def add(self, *, runs: int, priced_runs: int, cost_microusd: int) -> None:
        self.runs += int(runs)
        self.priced_runs += int(priced_runs)
        self.cost_microusd += int(cost_microusd)


@dataclass
class OpeningAndCatchup:
    """One scope's spend, opening rotation and season catch-up apart."""

    opening: RotationSpend = field(default_factory=RotationSpend)
    catchup: RotationSpend = field(default_factory=RotationSpend)


async def _spend(
    session: AsyncSession, scope: str, params: dict[str, object]
) -> dict[str | None, OpeningAndCatchup]:
    rows = await session.execute(
        text(_SPEND_SQL.format(scope=scope)),
        {
            "event": EVENT_SCORE_RETEST_REQUESTED,
            "basis": DUELING_ROTATION_RETEST_BASIS,
            **params,
        },
    )
    out: dict[str | None, OpeningAndCatchup] = {}
    for row in rows.mappings():
        bucket = out.setdefault(row["season_id"], OpeningAndCatchup())
        (bucket.catchup if row["catch_up"] else bucket.opening).add(
            runs=row["runs"],
            priced_runs=row["priced_runs"],
            cost_microusd=row["cost_microusd"],
        )
    return out


async def rotation_spend_by_season(
    session: AsyncSession, season_ids: Iterable[UUID]
) -> dict[UUID, OpeningAndCatchup]:
    """Spend of the rotation re-tests each season queued, by season."""
    ids = sorted({str(season_id) for season_id in season_ids})
    if not ids:
        return {}
    by_season = await _spend(
        session,
        "a.payload->>'dueling_season_id' = ANY(:season_ids)",
        {"season_ids": ids},
    )
    return {
        UUID(season_id): spend
        for season_id, spend in by_season.items()
        if season_id is not None
    }


async def rotation_spend_since(
    session: AsyncSession, since: datetime
) -> OpeningAndCatchup:
    """Spend of every rotation re-test lease started at or after ``since``."""
    total = OpeningAndCatchup()
    for spend in (
        await _spend(session, "a.recorded_at >= :since", {"since": since})
    ).values():
        for mine, theirs in (
            (total.opening, spend.opening),
            (total.catchup, spend.catchup),
        ):
            mine.add(
                runs=theirs.runs,
                priced_runs=theirs.priced_runs,
                cost_microusd=theirs.cost_microusd,
            )
    return total

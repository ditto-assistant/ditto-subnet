"""The generator scorer's advertised capability, and the guards that use it.

The scorer (``services/dittobench-api/cmd/challenge-scorer``) sends its
version, build revision, sandbox and the case bounds its pack contract accepts
(``challenge.MinCases``/``MaxCases``) as query parameters on every work poll.
Platform keeps the latest per scorer in ``generator_scorer_heartbeats`` and:

* leases no qualification whose ``cases_per_seed`` the polling scorer refuses;
* voids, with an ``infra:`` reason, a round whose ``cases_per_column`` the
  polling scorer refuses (when leased) or the live scorer refuses (at open);
* refuses a settings write whose ``cases_per_seed`` the live scorer refuses,
  unless the operator overrides it.

A scorer older than advertising sends no bounds; Platform then cannot know and
does not refuse (the scorer reports a refused request as a fault instead).
On 2026-10-27 a stale scorer (MaxCases 64, cases_per_seed 70) voided round
25968 and rejected a generator; this is the guard that would have stopped it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import GeneratorScorerHeartbeat

# A scorer seen within this window is "live": its bounds gate settings writes
# and round opens. Polls are every 30s when idle, but one generation or
# qualification can hold the scorer for many minutes.
SCORER_CAPABILITY_TTL = timedelta(hours=6)

Poll = Literal["qualification", "round"]


@dataclass(frozen=True)
class ScorerCapability:
    """What one poll advertised."""

    scorer_id: str
    scorer_version: str
    min_cases: int | None = None
    max_cases: int | None = None
    sandbox: str | None = None
    build_revision: str | None = None

    @property
    def known(self) -> bool:
        return self.min_cases is not None and self.max_cases is not None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def refusal(
    scorer: ScorerCapability | GeneratorScorerHeartbeat | None,
    cases: int,
    *,
    what: str = "cases_per_seed",
) -> str | None:
    """Why ``scorer`` cannot run ``cases`` cases per pack, or ``None``.

    ``None`` too when there is no scorer or it advertised no bounds: Platform
    cannot know, so it does not refuse.
    """
    if scorer is None or scorer.min_cases is None or scorer.max_cases is None:
        return None
    if scorer.min_cases <= cases <= scorer.max_cases:
        return None
    return (
        f"generator scorer {scorer.scorer_id} ({scorer.scorer_version}) supports "
        f"{scorer.min_cases}..{scorer.max_cases} cases per pack; {what} is {cases}"
    )


async def record_heartbeat(
    session: AsyncSession, capability: ScorerCapability, *, poll: Poll, now: datetime
) -> None:
    """Upsert the scorer's latest advertisement (caller commits)."""
    values = {
        "scorer_version": capability.scorer_version,
        "min_cases": capability.min_cases,
        "max_cases": capability.max_cases,
        "sandbox": capability.sandbox,
        "build_revision": capability.build_revision,
        "last_poll": poll,
        "last_seen_at": now,
    }
    statement = insert(GeneratorScorerHeartbeat).values(
        scorer_id=capability.scorer_id, first_seen_at=now, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[GeneratorScorerHeartbeat.scorer_id], set_=values
        )
    )


async def list_heartbeats(session: AsyncSession) -> list[GeneratorScorerHeartbeat]:
    return list(
        await session.scalars(
            select(GeneratorScorerHeartbeat).order_by(
                GeneratorScorerHeartbeat.last_seen_at.desc(),
                GeneratorScorerHeartbeat.scorer_id,
            )
        )
    )


def is_live(row: GeneratorScorerHeartbeat, *, now: datetime) -> bool:
    return _aware(row.last_seen_at) >= now - SCORER_CAPABILITY_TTL


async def live_scorer(
    session: AsyncSession, *, now: datetime
) -> GeneratorScorerHeartbeat | None:
    """The most recently seen scorer inside :data:`SCORER_CAPABILITY_TTL`."""
    return await session.scalar(
        select(GeneratorScorerHeartbeat)
        .where(GeneratorScorerHeartbeat.last_seen_at >= now - SCORER_CAPABILITY_TTL)
        .order_by(GeneratorScorerHeartbeat.last_seen_at.desc())
        .limit(1)
    )

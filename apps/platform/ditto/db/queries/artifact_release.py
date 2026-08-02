"""Derive retroactive public-source release times from accepted scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Score


@dataclass(frozen=True)
class ArtifactScoreQuorum:
    """The first benchmark-version quorum completed by one submission."""

    agent_id: UUID
    bench_version: int
    finalized_at: datetime


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps to the Postgres UTC contract."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def list_first_score_quorums(
    session: AsyncSession,
    *,
    agent_ids: list[UUID] | set[UUID] | tuple[UUID, ...],
    quorum: int,
) -> dict[UUID, ArtifactScoreQuorum]:
    """Return each agent's earliest completed same-version score quorum.

    ``Score.created_at`` is the platform-controlled first-insert time. It does
    not move when a validator re-scores the same agent/version, so the third
    row is a stable, retroactive record of when 3/3 was first reached. Scores
    from different benchmark versions never combine into a quorum.

    Invariant this depends on: no code path deletes and re-inserts a score row
    (``upsert_score`` updates in place). A delete + insert would move
    ``created_at`` and silently shift a published release time.
    """
    if not agent_ids:
        return {}

    ranked = (
        select(
            Score.agent_id.label("agent_id"),
            Score.bench_version.label("bench_version"),
            Score.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=(Score.agent_id, Score.bench_version),
                order_by=(Score.created_at.asc(), Score.validator_hotkey.asc()),
            )
            .label("score_number"),
        )
        .where(Score.agent_id.in_(agent_ids))
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ranked.c.agent_id,
                ranked.c.bench_version,
                ranked.c.created_at,
            )
            .where(ranked.c.score_number == quorum)
            .order_by(
                ranked.c.created_at.asc(),
                ranked.c.bench_version.asc(),
                ranked.c.agent_id.asc(),
            )
        )
    ).all()

    result: dict[UUID, ArtifactScoreQuorum] = {}
    for agent_id, bench_version, finalized_at in rows:
        result.setdefault(
            agent_id,
            ArtifactScoreQuorum(
                agent_id=agent_id,
                bench_version=int(bench_version),
                finalized_at=_as_utc(finalized_at),
            ),
        )
    return result

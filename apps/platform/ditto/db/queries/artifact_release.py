"""Derive retroactive public-source release times from accepted scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, AgentKingship, Score
from ditto.db.queries.artifact_release_settings import ArtifactReleasePolicy
from ditto.db.queries.king_reign import get_king_reveal


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


async def list_public_source_releases(
    session: AsyncSession,
    *,
    agent_ids: list[UUID] | set[UUID] | tuple[UUID, ...],
    quorum: int,
    policy: ArtifactReleasePolicy,
) -> dict[UUID, datetime]:
    """Return ``agent_id -> when its source became publicly downloadable``.

    The same predicate the public routes serve, expressed for consumers that
    need the *fact* of publication rather than a wire projection: source release
    is **king-only** and its clock starts at on-chain weight confirmation, not at
    upload and not at score quorum. An agent is present here only when it (1)
    completed a same-version score quorum, (2) held the crown and had validator
    weights confirmed on-chain, and (3) is currently ``scored`` or ``live``;
    ``available_at`` is ``weight_confirmed_at + policy.embargo_hours``. Agents
    that never reigned — the overwhelming majority — are simply absent.

    ``policy`` is passed in rather than read here so the caller controls *which*
    revision applies. The anti-copy gate passes the revision that was in force
    when the candidate was uploaded; a live read passes the current one.

    Two deliberate conservatisms, both of which can only *omit* a release and so
    can only preserve a copy hold, never create a false exemption:

    * status is read as it stands now, so an artifact that was downloadable and
      has since been banned or reopened for review does not count;
    * one policy revision is applied to the whole window, so a release that
      opened under a shorter embargo and was re-closed by a later, longer one is
      not credited to the shorter revision.
    """
    if not agent_ids or not policy.releases_publicly:
        return {}

    # Kings first. `agent_kingship` is tiny and indexed on the primary key,
    # whereas the quorum lookup windows over every score row of every id it is
    # given -- and this runs on the score-finalization path with the whole
    # eligible ledger as input. Narrowing to confirmed kings before asking about
    # quorums keeps the expensive query proportional to the ~30 artifacts that
    # have ever been published rather than to the ledger.
    reveals = await get_king_reveal(session, agent_ids=list(agent_ids))
    confirmed = {
        agent_id: reveal.weight_confirmed_at
        for agent_id, reveal in reveals.items()
        if reveal.weight_confirmed_at is not None
    }
    if not confirmed:
        return {}
    quorums = await list_first_score_quorums(
        session, agent_ids=list(confirmed), quorum=quorum
    )
    confirmed = {
        agent_id: weight_confirmed_at
        for agent_id, weight_confirmed_at in confirmed.items()
        if agent_id in quorums
    }
    if not confirmed:
        return {}
    releasable = set(
        (
            await session.execute(
                select(Agent.agent_id).where(
                    Agent.agent_id.in_(confirmed),
                    Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
                )
            )
        )
        .scalars()
        .all()
    )
    window = timedelta(hours=policy.embargo_hours)
    return {
        agent_id: _as_utc(weight_confirmed_at) + window
        for agent_id, weight_confirmed_at in confirmed.items()
        if agent_id in releasable
    }


async def available_public_source_agent_ids(
    session: AsyncSession,
    *,
    quorum: int,
    policy: ArtifactReleasePolicy,
    now: datetime,
) -> set[UUID]:
    """Return currently downloadable source ids without scanning submissions.

    Kingship is intentionally the leading relation: it is the tiny eligibility
    ledger, while ``agents`` is the unbounded public activity history. Only the
    already-confirmed, embargo-complete kings reach the score-quorum window.
    """
    if not policy.releases_publicly:
        return set()
    cutoff = now - timedelta(hours=policy.embargo_hours)
    candidates = set(
        await session.scalars(
            select(AgentKingship.agent_id)
            .join(Agent, Agent.agent_id == AgentKingship.agent_id)
            .where(
                AgentKingship.weight_confirmed_at.is_not(None),
                AgentKingship.weight_confirmed_at <= cutoff,
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
            )
        )
    )
    if not candidates:
        return set()
    quorums = await list_first_score_quorums(
        session, agent_ids=candidates, quorum=quorum
    )
    return set(quorums)

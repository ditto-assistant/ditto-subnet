"""King-reign ledger for gating public source release.

Public source release is king-only and gated in two stages:

1. **Ever king** -- the agent was observed as the KOTH champion at least once
   (``first_crowned_at``, write-once). This is eligibility.
2. **On-chain weight confirmed** -- validators' REVEALED weights (post
   commit-reveal) have been set on this miner (``weight_confirmed_at``,
   write-once). Only then does the public window open; it is measured from
   this instant.

Erring toward weights (not realized emission magnitude) keeps a genuine king
from being trapped private: the revealed ``Weights`` matrix is the earliest
commit-reveal-gated proof that validators backed the miner. Recording happens
on the validator score path; the public gate here only reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentKingship


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps to the Postgres UTC contract."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class KingReveal:
    """Reveal state for an agent that has held the KOTH crown.

    ``weight_confirmed_at`` is ``None`` until validators' revealed on-chain
    weights are first seen set on this miner; the public window is measured
    from it once present.
    """

    first_crowned_at: datetime
    weight_confirmed_at: datetime | None


async def record_first_crowned(
    session: AsyncSession, *, agent_id: UUID, now: datetime
) -> None:
    """Record an agent's first coronation. Idempotent and write-once.

    A later re-coronation must NOT move ``first_crowned_at``: it is the
    eligibility marker, not the release clock. Callers run this in a
    best-effort, isolated transaction, so a duplicate race (two validators
    crowning the same champion at once) is a harmless no-op.
    """
    if await session.get(AgentKingship, agent_id) is not None:
        return
    session.add(AgentKingship(agent_id=agent_id, first_crowned_at=now))


async def record_weight_confirmed(
    session: AsyncSession, *, agent_id: UUID, now: datetime
) -> None:
    """Stamp the first on-chain weight confirmation for an ever-king agent.

    Write-once and safe to call repeatedly: a no-op unless the agent is a
    known king with no confirmation yet. Anchors the public 48h window.
    """
    row = await session.get(AgentKingship, agent_id)
    if row is None or row.weight_confirmed_at is not None:
        return
    row.weight_confirmed_at = now


async def list_unconfirmed_kings(session: AsyncSession) -> list[tuple[UUID, str]]:
    """Return ``(agent_id, miner_hotkey)`` for ever-kings not yet weight-confirmed.

    The caller matches each hotkey against the revealed weight matrix to decide
    whether to confirm. Usually a very small set (kings are rare), so this stays
    cheap to sweep on the score path.
    """
    rows = (
        await session.execute(
            select(AgentKingship.agent_id, Agent.miner_hotkey)
            .join(Agent, Agent.agent_id == AgentKingship.agent_id)
            .where(AgentKingship.weight_confirmed_at.is_(None))
        )
    ).all()
    return [(agent_id, hotkey) for agent_id, hotkey in rows]


async def get_king_reveal(
    session: AsyncSession,
    *,
    agent_ids: list[UUID] | set[UUID] | tuple[UUID, ...],
) -> dict[UUID, KingReveal]:
    """Return the reveal state for agents that have held the crown."""
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AgentKingship.agent_id,
                AgentKingship.first_crowned_at,
                AgentKingship.weight_confirmed_at,
            ).where(AgentKingship.agent_id.in_(agent_ids))
        )
    ).all()
    return {
        agent_id: KingReveal(
            first_crowned_at=_as_utc(first_crowned_at),
            weight_confirmed_at=(
                _as_utc(weight_confirmed_at)
                if weight_confirmed_at is not None
                else None
            ),
        )
        for agent_id, first_crowned_at, weight_confirmed_at in rows
    }

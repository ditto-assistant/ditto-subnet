"""Lineage parent selection for screening attempts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent


async def select_lineage_parent(session: AsyncSession, agent: Agent) -> Agent | None:
    """The same hotkey's latest earlier version of this agent name, if any.

    ``(miner_hotkey, name, version)`` is unique, so this is one index probe.
    Ownership stays at the hotkey: a renamed or re-keyed agent starts a new
    lineage and gets an ordinary full review.
    """
    if agent.version is None:
        return None
    return await session.scalar(
        select(Agent)
        .where(
            Agent.miner_hotkey == agent.miner_hotkey,
            Agent.name == agent.name,
            Agent.version.is_not(None),
            Agent.version < agent.version,
        )
        .order_by(Agent.version.desc())
        .limit(1)
    )


__all__ = ["select_lineage_parent"]

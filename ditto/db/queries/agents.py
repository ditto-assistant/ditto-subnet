"""Queries against the ``agents`` table.

Writes (``insert_agent``) and reads (``get_latest_agent_by_hotkey``,
``get_agent_by_id``) sit together because the table is small and the
two surfaces share their dispatch on the ORM model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import Agent

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def insert_agent(
    session: AsyncSession,
    *,
    agent_id: UUID,
    miner_hotkey: str,
    name: str,
    sha256: str,
) -> None:
    """Insert one ``agents`` row inside the caller-owned transaction.

    Status is omitted so the schema default ``'uploaded'`` applies; the
    screener PR moves it forward through the state machine. The caller
    runs this together with :func:`insert_evaluation_payment` inside one
    ``async with session.begin():`` block so both rows commit atomically
    (a PK violation on the payment insert rolls the agent insert back).

    Raises:
        DbIntegrityError: Any constraint violation on ``agents``
            (UNIQUE ``(agent_id, miner_hotkey)``, NOT NULL violations,
            invalid enum value, etc.). No agents-level constraint is a
            miner-facing action, so the envelope catch-all maps every
            case to HTTP 500.
    """
    row = Agent(
        agent_id=agent_id,
        miner_hotkey=miner_hotkey,
        name=name,
        sha256=sha256,
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as e:
        raise DbIntegrityError(f"agents insert violated constraint: {e.orig}") from e


async def get_latest_agent_by_hotkey(
    session: AsyncSession,
    *,
    miner_hotkey: str,
) -> Agent | None:
    """Return the most recent ``agents`` row for the given hotkey, or ``None``.

    Orders by ``created_at DESC`` and takes one. Status is unfiltered;
    callers see banned or failed rows if they are the most recent. Per
    the retrieval design, hotkey-level banned surfacing is deferred to
    the ban PR (Phase 5) where the ``banned_hotkeys`` table lands.
    """
    stmt = (
        select(Agent)
        .where(Agent.miner_hotkey == miner_hotkey)
        .order_by(Agent.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_agent_by_id(
    session: AsyncSession,
    *,
    agent_id: UUID,
) -> Agent | None:
    """Return the ``agents`` row for the given id, or ``None``."""
    return await session.get(Agent, agent_id)

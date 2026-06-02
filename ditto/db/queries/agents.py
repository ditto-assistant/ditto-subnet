"""Mutations against the ``agents`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    ip_address: str | None,
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
        ip_address=ip_address,
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as e:
        raise DbIntegrityError(f"agents insert violated constraint: {e.orig}") from e

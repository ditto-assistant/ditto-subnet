"""Queries against the ``banned_hotkeys`` table (hotkey-level bans).

A ban here blocks a *miner* (all future uploads), distinct from the per-agent
:attr:`AgentStatus.BANNED` status that rejects a single submission. The read
(:func:`is_hotkey_banned`) is on the upload hot path, so it is a single indexed
PK lookup; the writes back the owner-only ``scripts/ban_hotkey.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import BannedHotkey

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def is_hotkey_banned(session: AsyncSession, *, hotkey: str) -> bool:
    """Return ``True`` iff ``hotkey`` has a ban row. Single PK lookup."""
    stmt = select(BannedHotkey.hotkey).where(BannedHotkey.hotkey == hotkey)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def ban_hotkey(
    session: AsyncSession, *, hotkey: str, reason: str | None = None
) -> bool:
    """Insert a ban row inside the caller-owned transaction.

    Returns ``True`` if a new ban was recorded, ``False`` if the hotkey was
    already banned (idempotent — the existing reason/timestamp is preserved).
    """
    if await is_hotkey_banned(session, hotkey=hotkey):
        return False
    session.add(BannedHotkey(hotkey=hotkey, reason=reason))
    try:
        await session.flush()
    except SAIntegrityError as e:  # pragma: no cover - raced concurrent insert
        raise DbIntegrityError(
            f"banned_hotkeys insert violated constraint: {e.orig}"
        ) from e
    return True


async def unban_hotkey(session: AsyncSession, *, hotkey: str) -> bool:
    """Remove a ban row. Returns ``True`` if a row existed, else ``False``.

    An owner-only cold path, so it reads-then-deletes for a clean boolean
    (avoids depending on the driver's ``rowcount``).
    """
    existed = await is_hotkey_banned(session, hotkey=hotkey)
    if existed:
        await session.execute(delete(BannedHotkey).where(BannedHotkey.hotkey == hotkey))
    return existed

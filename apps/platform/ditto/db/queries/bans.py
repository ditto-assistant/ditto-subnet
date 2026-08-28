"""Queries against the ``banned_hotkeys`` table (hotkey-level bans).

A ban here blocks a *miner* (all future uploads), distinct from the per-agent
:attr:`AgentStatus.BANNED` status that rejects a single submission. The read
(:func:`is_hotkey_banned`) is on the upload hot path, so it is a single indexed
PK lookup; the writes back the owner-only ``scripts/ban_hotkey.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import BannedHotkey, HotkeyBanAudit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def is_hotkey_banned(session: AsyncSession, *, hotkey: str) -> bool:
    """Return ``True`` iff ``hotkey`` has a ban row. Single PK lookup."""
    stmt = select(BannedHotkey.hotkey).where(BannedHotkey.hotkey == hotkey)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def get_hotkey_ban(
    session: AsyncSession, *, hotkey: str, for_update: bool = False
) -> BannedHotkey | None:
    """Return the exact active ban row, optionally locking it for mutation."""
    stmt = select(BannedHotkey).where(BannedHotkey.hotkey == hotkey)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def count_hotkey_bans(session: AsyncSession) -> int:
    """Count every active hotkey-level upload ban."""
    return int(
        await session.scalar(select(func.count()).select_from(BannedHotkey)) or 0
    )


async def list_hotkey_bans(
    session: AsyncSession, *, limit: int, offset: int
) -> list[BannedHotkey]:
    """List active bans newest first with a stable hotkey tiebreaker."""
    return list(
        await session.scalars(
            select(BannedHotkey)
            .order_by(BannedHotkey.banned_at.desc(), BannedHotkey.hotkey.asc())
            .limit(limit)
            .offset(offset)
        )
    )


async def list_hotkey_ban_audit(
    session: AsyncSession, *, hotkey: str, limit: int
) -> list[HotkeyBanAudit]:
    """List newest-first operator actions for one hotkey."""
    return list(
        await session.scalars(
            select(HotkeyBanAudit)
            .where(HotkeyBanAudit.hotkey == hotkey)
            .order_by(HotkeyBanAudit.recorded_at.desc(), HotkeyBanAudit.seq.desc())
            .limit(limit)
        )
    )


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


async def unban_hotkey_with_audit(
    session: AsyncSession,
    *,
    hotkey: str,
    expected_banned_at: datetime,
    actor: str,
    reason: str,
) -> HotkeyBanAudit | None:
    """Remove one exact active ban and append its operator audit atomically.

    ``None`` means the hotkey was not banned when the row lock was acquired.
    A mismatched timestamp is left for the endpoint to report as a stale
    concurrency guard without deleting anything.
    """
    row = await get_hotkey_ban(session, hotkey=hotkey, for_update=True)
    if row is None or row.banned_at != expected_banned_at:
        return None
    audit = HotkeyBanAudit(
        hotkey=hotkey,
        action="unban",
        actor=actor,
        reason=reason,
        previous_reason=row.reason,
        previous_banned_at=row.banned_at,
    )
    session.add(audit)
    await session.delete(row)
    await session.flush()
    return audit

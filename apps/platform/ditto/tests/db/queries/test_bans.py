"""Unit tests for :mod:`ditto.db.queries.bans` against SQLite-in-memory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.bans import ban_hotkey, is_hotkey_banned, unban_hotkey

_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_OTHER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


async def test_unknown_hotkey_is_not_banned(session: AsyncSession) -> None:
    assert await is_hotkey_banned(session, hotkey=_HOTKEY) is False


async def test_ban_then_query(session: AsyncSession) -> None:
    async with session.begin():
        added = await ban_hotkey(session, hotkey=_HOTKEY, reason="confirmed copy")
    assert added is True
    assert await is_hotkey_banned(session, hotkey=_HOTKEY) is True
    # A different hotkey is unaffected.
    assert await is_hotkey_banned(session, hotkey=_OTHER) is False


async def test_ban_is_idempotent(session: AsyncSession) -> None:
    async with session.begin():
        assert await ban_hotkey(session, hotkey=_HOTKEY, reason="first") is True
    async with session.begin():
        # Second ban is a no-op (already banned); the original reason survives.
        assert await ban_hotkey(session, hotkey=_HOTKEY, reason="second") is False
    assert await is_hotkey_banned(session, hotkey=_HOTKEY) is True


async def test_unban_removes_ban(session: AsyncSession) -> None:
    async with session.begin():
        await ban_hotkey(session, hotkey=_HOTKEY)
    async with session.begin():
        assert await unban_hotkey(session, hotkey=_HOTKEY) is True
    assert await is_hotkey_banned(session, hotkey=_HOTKEY) is False


async def test_unban_unknown_hotkey_is_false(session: AsyncSession) -> None:
    async with session.begin():
        assert await unban_hotkey(session, hotkey=_HOTKEY) is False

"""Real-Postgres coverage for the append-only epoch pin table."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.queries.ledger_epochs import (
    LedgerPinDraft,
    get_pin,
    insert_pin,
    latest_pin,
    list_pins,
)


def _draft(epoch_index: int, *, netuid: int = 118) -> LedgerPinDraft:
    return LedgerPinDraft(
        netuid=netuid,
        epoch_index=epoch_index,
        last_epoch_block=epoch_index * 360,
        pinned_block=epoch_index * 360 + 2,
        pinned_block_hash="0x" + "ab" * 32,
        pinned_at=datetime(2026, 9, 10, tzinfo=UTC),
        bench_version=12,
        entries=[{"agent_id": str(uuid4()), "miner_hotkey": "5" + "A" * 47}],
        context={"served": {"crown_mode": None}},
        ledger_digest="cd" * 32,
        champion_agent_id=uuid4(),
        champion_owner_root="owner:a",
    )


async def test_pins_are_listed_newest_first_and_looked_up_exactly(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        for index in (25_026, 25_028, 25_027):
            await insert_pin(session, _draft(index))
        await insert_pin(session, _draft(3, netuid=119))
    async with session_maker() as session:
        newest = await latest_pin(session, netuid=118)
        assert newest is not None and newest.epoch_index == 25_028
        capped = await latest_pin(session, netuid=118, max_epoch_index=25_027)
        assert capped is not None and capped.epoch_index == 25_027
        exact = await get_pin(session, netuid=118, epoch_index=25_026)
        assert exact is not None and exact.champion_owner_root == "owner:a"
        assert await get_pin(session, netuid=118, epoch_index=25_030) is None
        rows = await list_pins(session, netuid=118, limit=2)
        assert [row.epoch_index for row in rows] == [25_028, 25_027]
        other = await latest_pin(session, netuid=119)
        assert other is not None and other.epoch_index == 3


async def test_duplicate_epoch_is_refused_at_flush(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await insert_pin(session, _draft(25_030))
    async with session_maker() as session:
        async with session.begin():
            with pytest.raises(IntegrityError):
                await insert_pin(session, _draft(25_030))
        await session.rollback()
    async with session_maker() as session:
        winner = await get_pin(session, netuid=118, epoch_index=25_030)
        assert winner is not None

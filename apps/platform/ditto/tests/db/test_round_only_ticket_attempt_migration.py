"""Letting ``validator_tickets.attempt_count`` be zero round-trips (SN-334).

Upgrade admits the zero-attempt row a generator round creates, with the new
CHECK validated. Downgrade restores ``> 0`` by recording the round's lease as
the one attempt it used to be charged as, and re-upgrading is clean.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "8b3d6f0c2a71"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _seed_agent(engine: AsyncEngine) -> UUID:
    agent_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO agents (agent_id, miner_hotkey, name, version, sha256, "
                "size_bytes, status) VALUES (:agent_id, :hotkey, 'round-only', 1, "
                ":sha, 1024, 'scored')"
            ),
            {
                "agent_id": agent_id,
                "hotkey": f"5RoundOnly{agent_id.hex[:8]}",
                "sha": agent_id.hex * 2,
            },
        )
    return agent_id


async def _insert_ticket(
    engine: AsyncEngine, *, agent_id: UUID, hotkey: str, attempts: int
) -> None:
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validator_tickets (agent_id, bench_version, "
                "validator_hotkey, slot_id, status, purpose, purpose_revision, "
                "legacy_completion_allowed, issued_at, deadline, attempt_count) "
                "VALUES (:agent_id, 13, :hotkey, 'slot-0', 'expired', "
                "'generator_round', 1, false, :issued, :deadline, :attempts)"
            ),
            {
                "agent_id": agent_id,
                "hotkey": hotkey,
                "issued": now - timedelta(hours=2),
                "deadline": now - timedelta(hours=1),
                "attempts": attempts,
            },
        )


async def _attempts(engine: AsyncEngine, *, agent_id: UUID, hotkey: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(
                    "SELECT attempt_count FROM validator_tickets "
                    "WHERE agent_id = :agent_id AND validator_hotkey = :hotkey"
                ),
                {"agent_id": agent_id, "hotkey": hotkey},
            )
        )


async def _validated(engine: AsyncEngine, name: str) -> bool | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT convalidated FROM pg_constraint WHERE conname = :name "
                "AND conrelid = 'validator_tickets'::regclass"
            ),
            {"name": name},
        )


async def test_zero_attempt_round_rows_round_trip(engine: AsyncEngine) -> None:
    agent_id = await _seed_agent(engine)
    try:
        assert await _validated(engine, "validator_tickets_attempt_count_nonnegative")
        assert await _validated(engine, "validator_tickets_attempt_count_positive") is (
            None
        )
        await _insert_ticket(engine, agent_id=agent_id, hotkey="5Round", attempts=0)
        with pytest.raises(IntegrityError):
            await _insert_ticket(
                engine, agent_id=agent_id, hotkey="5Negative", attempts=-1
            )

        _alembic("downgrade", _PARENT)
        assert await _attempts(engine, agent_id=agent_id, hotkey="5Round") == 1
        assert await _validated(engine, "validator_tickets_attempt_count_positive")
        with pytest.raises(IntegrityError):
            await _insert_ticket(
                engine, agent_id=agent_id, hotkey="5ZeroOld", attempts=0
            )

        _alembic("upgrade", "head")
        assert await _validated(engine, "validator_tickets_attempt_count_nonnegative")
        await _insert_ticket(engine, agent_id=agent_id, hotkey="5ZeroNew", attempts=0)
    finally:
        _alembic("upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM agents WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )

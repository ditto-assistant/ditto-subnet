"""The generator competition tables round-trip and enforce their invariants."""

from __future__ import annotations

import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "73bd92c14f60"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _insert_pack(engine: AsyncEngine, **overrides: object) -> None:
    values: dict[str, object] = {
        "pack_id": str(uuid4()),
        "sha": "a" * 64,
        "status": "queued",
        "bytes": b"{}",
        "size": 2,
        "lease_id": None,
    }
    values.update(overrides)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO generator_packs (pack_id, miner_hotkey, owner_key, "
                "pack_sha256, pack_bytes, size_bytes, case_count, generator_name, "
                "generator_version, status, not_before, lease_id) VALUES "
                "(CAST(:pack_id AS uuid), 'hk', 'hotkey:hk', :sha, :bytes, :size, 8, "
                "'g', '1', :status, now(), CAST(:lease_id AS uuid))"
            ),
            values,
        )


async def _clear(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM generator_packs"))


async def test_generator_competition_round_trip(engine: AsyncEngine) -> None:
    try:
        await _clear(engine)
        await _insert_pack(engine)
        with pytest.raises(IntegrityError):  # duplicate identity
            await _insert_pack(engine)
        with pytest.raises(IntegrityError):  # size must match the bytes
            await _insert_pack(engine, sha="b" * 64, size=99)
        with pytest.raises(IntegrityError):  # leased rows carry a lease
            await _insert_pack(engine, sha="c" * 64, status="leased")
        with pytest.raises(IntegrityError):  # unknown status
            await _insert_pack(engine, sha="d" * 64, status="limbo")
        await _clear(engine)

        _alembic("downgrade", _PARENT)
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT to_regclass('public.generator_packs')")
            )
        assert exists is None
        _alembic("upgrade", "head")
        await _insert_pack(engine, sha="e" * 64)
    finally:
        await _clear(engine)
        _alembic("upgrade", "head")

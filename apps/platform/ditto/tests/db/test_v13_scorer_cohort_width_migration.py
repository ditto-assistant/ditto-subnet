"""Widening the V13 scorer cohort to 3..16 members round-trips.

Upgrade must admit a four- and a sixteen-member pin or rotation and still
refuse two or seventeen. Downgrade restores the exact three-member check, and
refuses to run while a wider (immutable) row exists rather than failing
half-way through.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "3f1c9a7d2b6e"
_PACKET = json.dumps({"source_revision": "a" * 40})


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


def _hotkeys(count: int) -> str:
    return json.dumps([f"5member{index:02d}" for index in range(count)])


async def _insert_pin(engine: AsyncEngine, *, count: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v13_scorer_cohort_pins (bench_version, hotkeys, packet, "
                "slot_settings_revision, slot_settings_checksum, reason, actor) "
                "VALUES (13, CAST(:hotkeys AS jsonb), CAST(:packet AS jsonb), 1, "
                ":checksum, 'migration width test', 'test')"
            ),
            {"hotkeys": _hotkeys(count), "packet": _PACKET, "checksum": "f" * 64},
        )


async def _insert_rotation(engine: AsyncEngine, *, count: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO v13_scorer_cohort_rotations (bench_version, hotkeys, "
                "packet, previous_packet, slot_settings_revision, "
                "slot_settings_checksum, reason, actor) VALUES (13, "
                "CAST(:hotkeys AS jsonb), CAST(:packet AS jsonb), "
                "CAST(:packet AS jsonb), 1, :checksum, 'migration width test', "
                "'test')"
            ),
            {"hotkeys": _hotkeys(count), "packet": _PACKET, "checksum": "f" * 64},
        )


async def _clear(engine: AsyncEngine) -> None:
    # Both tables reject UPDATE and DELETE; TRUNCATE is how the harness resets.
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE v13_scorer_cohort_rotations, v13_scorer_cohort_pins")
        )


async def test_cohort_width_round_trip(engine: AsyncEngine) -> None:
    try:
        await _clear(engine)
        for count in (4, 16):
            await _insert_pin(engine, count=count)
            await _insert_rotation(engine, count=count)
            await _clear(engine)
        for count in (2, 17):
            with pytest.raises(IntegrityError):
                await _insert_pin(engine, count=count)
            with pytest.raises(IntegrityError):
                await _insert_rotation(engine, count=count)

        await _insert_rotation(engine, count=4)
        with pytest.raises(subprocess.CalledProcessError) as refused:
            _alembic("downgrade", _PARENT)
        assert "other than three members" in refused.value.stderr
        await _clear(engine)

        _alembic("downgrade", _PARENT)
        with pytest.raises(IntegrityError):
            await _insert_pin(engine, count=4)
        with pytest.raises(IntegrityError):
            await _insert_rotation(engine, count=4)
        await _insert_pin(engine, count=3)
        await _insert_rotation(engine, count=3)
        await _clear(engine)

        _alembic("upgrade", "head")
        await _insert_rotation(engine, count=4)
    finally:
        # Keep this worker database usable even when an assertion above fails.
        await _clear(engine)
        _alembic("upgrade", "head")

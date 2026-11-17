"""The readmission payment lane migration round-trips (SN-485).

The credit-origin CHECK is added ``NOT VALID`` and validated in its own
transaction through raw SQL, so this pins that it carries the conventional
name the model resolves to, is validated, enforces, and drops cleanly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "9d4b7e2a6c18"
_PLATFORM_DIR = Path(__file__).resolve().parents[3]
_CHECK = "ck_evaluation_payments_evaluation_payments_credit_origin_check"


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        # alembic.ini lives in apps/platform: run there wherever pytest started.
        cwd=_PLATFORM_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _check_state(engine: AsyncEngine) -> tuple[bool, bool] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT convalidated, pg_get_constraintdef(oid) LIKE "
                    "'%readmission%' FROM pg_constraint WHERE conname = :name"
                ),
                {"name": _CHECK},
            )
        ).first()
    return None if row is None else (bool(row[0]), bool(row[1]))


async def test_the_credit_origin_check_round_trips(engine: AsyncEngine) -> None:
    assert await _check_state(engine) == (True, True)
    try:
        _alembic("downgrade", _PARENT)
        assert await _check_state(engine) is None
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables WHERE tablename IN "
                            "('readmission_payments', 'readmission_reservations')"
                        )
                    )
                ).scalars()
            )
        assert tables == set()
    finally:
        _alembic("upgrade", "head")
    assert await _check_state(engine) == (True, True)

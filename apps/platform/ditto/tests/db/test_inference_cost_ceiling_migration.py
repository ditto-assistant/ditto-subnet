"""The #3271 cost-ceiling migration round-trips on the real chain."""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "4c8e2a6f1d93"
_GRANT_COLUMNS = {
    "ledger_cost_microusd",
    "cost_ceiling_microusd",
    "cost_ceiling_mode",
    "cost_ceiling_would_decline",
    "ledger_prompt_price_per_token",
    "ledger_completion_price_per_token",
    "embedding_price_microusd_per_token",
}
_CONSTRAINTS = {
    "ck_inference_grants_inference_grants_cost_ceiling_mode",
    "ck_inference_grants_inference_grants_cost_ceiling_microusd",
    "ck_inference_grants_inference_grants_ledger_cost_microusd",
    "ck_inference_grants_inference_grants_embedding_price",
    "ck_inference_requests_inference_requests_ledger_cost_microusd",
}


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ),
            {"table": table},
        )
        return {row[0] for row in rows}


async def _constraints(engine: AsyncEngine) -> dict[str, bool]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conname = ANY(:names)"
            ),
            {"names": sorted(_CONSTRAINTS)},
        )
        return {row[0]: row[1] for row in rows}


async def test_cost_ceiling_migration_round_trip(engine: AsyncEngine) -> None:
    try:
        _alembic("downgrade", _PARENT)
        assert not (await _columns(engine, "inference_grants")) & _GRANT_COLUMNS
        assert "ledger_cost_microusd" not in await _columns(
            engine, "inference_requests"
        )
        assert await _constraints(engine) == {}

        _alembic("upgrade", "head")
        assert await _columns(engine, "inference_grants") >= _GRANT_COLUMNS
        assert "ledger_cost_microusd" in await _columns(engine, "inference_requests")
        # Added NOT VALID, then validated: every constraint ends up enforced.
        assert await _constraints(engine) == dict.fromkeys(_CONSTRAINTS, True)

        # Re-running the upgrade body is a no-op (every step is idempotent).
        _alembic("downgrade", _PARENT)
        _alembic("upgrade", "head")
        assert await _constraints(engine) == dict.fromkeys(_CONSTRAINTS, True)
    finally:
        _alembic("upgrade", "head")

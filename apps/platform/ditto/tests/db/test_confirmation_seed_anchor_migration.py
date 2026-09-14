"""Upgrade/downgrade proof for the confirmation seed anchor table."""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "3c8f0a1d9b27"
_HEAD = "9e4b2f7c1a53"
_TABLE = "confirmation_seed_anchors"


def alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def public_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(
            await connection.run_sync(
                lambda sync: sync.dialect.get_table_names(sync, schema="public")
            )
        )


async def test_confirmation_seed_anchor_migration_round_trip(
    engine: AsyncEngine,
) -> None:
    """The migration adds only its own table and recreates every guard."""
    try:
        alembic("downgrade", _PARENT)
        assert _TABLE not in await public_tables(engine)

        alembic("upgrade", _HEAD)
        assert _TABLE in await public_tables(engine)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            # CHECK names are convention-prefixed and truncated to Postgres's
            # 63-char limit, so pin the definitions instead of the spellings.
            constraint_rows = (
                await connection.execute(
                    text(
                        "SELECT conname, contype::text, pg_get_constraintdef(oid) "
                        "FROM pg_constraint "
                        "WHERE conrelid = 'confirmation_seed_anchors'::regclass"
                    )
                )
            ).all()
            constraints = {row[0] for row in constraint_rows}
            check_definitions = [row[2] for row in constraint_rows if row[1] == "c"]
            indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public' "
                        "AND tablename = 'confirmation_seed_anchors'"
                    )
                )
            )
        assert revision == _HEAD
        assert len(check_definitions) == 4, check_definitions
        for fragment in (
            "bench_version > 0",
            "anchor_block > ready_block",
            "'^0x[0-9a-f]{64}$'",
            "pinned_at IS NULL",
        ):
            assert any(fragment in definition for definition in check_definitions), (
                fragment,
                check_definitions,
            )
        assert {
            "confirmation_seed_anchors_pkey",
            "confirmation_seed_anchors_champion_fkey",
        } <= constraints
        assert "confirmation_seed_anchors_version_idx" in indexes
    finally:
        # Keep this worker database usable even when an assertion above fails.
        alembic("upgrade", "head")

"""Upgrade/downgrade proof for the isolated confirmation-bundle schema."""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "e2b7c4a1d590"
_HEAD = "b4d9e7c2a601"
_TABLES = {
    "confirmation_bundle_settings_revisions",
    "confirmation_retest_authorizations",
    "confirmation_bundles",
    "confirmation_bundle_subjects",
    "confirmation_bundle_tickets",
    "confirmation_dimension_evidence",
    "confirmation_budget_days",
    "confirmation_budget_reservations",
}


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


async def test_confirmation_bundle_migration_round_trip(engine: AsyncEngine) -> None:
    """The migration removes only its own schema and recreates every guard."""
    try:
        alembic("downgrade", _PARENT)
        assert _TABLES.isdisjoint(await public_tables(engine))

        alembic("upgrade", _HEAD)
        assert await public_tables(engine) >= _TABLES
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            triggers = set(
                await connection.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE NOT tgisinternal AND tgname LIKE 'confirmation_%'"
                    )
                )
            )
            indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = 'public' "
                        "AND indexname LIKE 'confirmation_%'"
                    )
                )
            )
        assert revision == _HEAD
        assert {
            "confirmation_bundles_immutability_guard",
            "confirmation_dimension_evidence_append_only_guard",
            "confirmation_subject_authority_guard",
        } <= triggers
        assert {
            "confirmation_bundles_identity_key",
            "confirmation_tickets_one_live_bundle_idx",
            "confirmation_reservations_one_open_bundle_idx",
            "confirmation_settings_parent_key",
        } <= indexes
    finally:
        # Keep this worker database usable even when an assertion above fails.
        alembic("upgrade", "head")

"""The V13 policy-document migration can be safely reversed and reapplied."""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def test_v13_policy_document_digest_migration_round_trip(
    engine: AsyncEngine,
) -> None:
    try:
        _alembic("downgrade", "5c7e219a0d4b")
        async with engine.connect() as connection:
            columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'screening_review_deadline_activations'"
                    )
                )
            )
        assert "policy_document_digest" not in columns

        _alembic("upgrade", "head")
        async with engine.connect() as connection:
            constraints = set(
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = "
                        "'screening_review_deadline_activations'::regclass"
                    )
                )
            )
        assert "srda_document_digest_check" in constraints
    finally:
        _alembic("upgrade", "head")

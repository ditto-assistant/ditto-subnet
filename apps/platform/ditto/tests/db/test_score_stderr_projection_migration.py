"""Real PostgreSQL backfill, old-writer and derived-projection guarantees."""

import importlib.util
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine


def _probe(connection: Connection) -> None:
    path = (
        Path(__file__).parents[3]
        / "alembic/versions/2026_10_09_add_score_stderr_projection.py"
    )
    spec = importlib.util.spec_from_file_location("stderr_projection_migration", path)
    assert spec is not None and spec.loader is not None
    migration = cast(Any, importlib.util.module_from_spec(spec))
    spec.loader.exec_module(migration)
    schema = "stderr_probe_" + uuid4().hex
    connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
    connection.exec_driver_sql(f"SET search_path TO {schema}, public")
    connection.exec_driver_sql(
        "CREATE TABLE scores (id int, bench_version int, details jsonb)"
    )
    documents: list[object] = [
        None,
        {},
        [],
        {"composite_stderr": None},
        {"composite_stderr": True},
        {"composite_stderr": "0.1"},
        {"composite_stderr": {"untrusted": "private"}},
        {"composite_stderr": 0.125},
        {"composite_stderr": 2},
        {"composite_stderr": 10**310},
    ]
    try:
        for index, document in enumerate(documents):
            connection.execute(
                text("INSERT INTO scores VALUES (:id, 7, CAST(:details AS jsonb))"),
                {"id": index, "details": json.dumps(document)},
            )
        connection.exec_driver_sql(
            "INSERT INTO scores VALUES (100, 2, '{\"composite_stderr\":0.05}')"
        )
        connection.exec_driver_sql(
            "ALTER TABLE scores ADD CONSTRAINT retired_floor "
            "CHECK (bench_version >= 7) NOT VALID"
        )
        connection.commit()
        original = connection.exec_driver_sql(
            "SELECT id, details::text FROM scores ORDER BY id"
        ).all()
        connection.rollback()
        for _ in range(2):
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            mismatch = connection.exec_driver_sql(
                "SELECT count(*) FROM scores WHERE bench_version >= 7 "
                "AND stderr_projection "
                "IS DISTINCT FROM jsonb_build_object("
                "'composite_stderr', details->'composite_stderr')"
            ).scalar_one()
            assert mismatch == 0
            assert (
                connection.exec_driver_sql(
                    "SELECT stderr_projection FROM scores WHERE id=100"
                ).scalar_one()
                is None
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT id, details::text FROM scores ORDER BY id"
                ).all()
                == original
            )
            connection.rollback()
        # Old application inserts/updates do not mention the new column.
        connection.exec_driver_sql(
            "INSERT INTO scores (id, bench_version, details) VALUES "
            "(99, 7, '{\"composite_stderr\":0.25}'::jsonb)"
        )
        connection.exec_driver_sql(
            "UPDATE scores SET details = '{\"composite_stderr\":0.5}'::jsonb "
            "WHERE id=99"
        )
        # A separately supplied projection cannot become authoritative.
        connection.exec_driver_sql(
            "UPDATE scores SET stderr_projection = "
            "'{\"composite_stderr\":0.9}'::jsonb WHERE id=99"
        )
        assert (
            connection.exec_driver_sql(
                "SELECT stderr_projection->'composite_stderr' FROM scores WHERE id=99"
            ).scalar_one()
            == 0.5
        )
        connection.commit()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE "
                    "table_schema=:schema AND table_name='scores' "
                    "AND column_name='stderr_projection'"
                ),
                {"schema": schema},
            ).scalar_one()
            == 0
        )
        connection.rollback()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert (
            connection.exec_driver_sql(
                "SELECT stderr_projection->'composite_stderr' FROM scores WHERE id=99"
            ).scalar_one()
            == 0.5
        )
    finally:
        connection.rollback()
        connection.exec_driver_sql("SET search_path TO public")
        connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")
        connection.commit()


@pytest.mark.asyncio
async def test_score_stderr_projection_migration(engine: AsyncEngine) -> None:
    assert engine.dialect.name == "postgresql"
    async with engine.connect() as connection:
        await connection.run_sync(_probe)

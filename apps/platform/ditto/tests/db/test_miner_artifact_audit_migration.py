"""The expanded requester check retains miner history across rollback."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


async def test_miner_audit_upgrade_and_downgrade_preserve_history(engine):
    path = (
        Path(__file__).parents[3]
        / "alembic/versions/2026_10_13_add_miner_artifact_fetch_audit.py"
    )
    spec = importlib.util.spec_from_file_location("miner_audit_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def probe(connection):
        schema = "miner_audit_probe_" + uuid4().hex
        connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        connection.exec_driver_sql(f"SET LOCAL search_path TO {schema}, public")
        connection.exec_driver_sql(
            "CREATE TABLE artifact_fetch_audit (requester_kind text NOT NULL, "
            "requester_id text, CONSTRAINT artifact_fetch_audit_requester_kind_check "
            "CHECK (requester_kind IN ('validator', 'screener', 'admin', 'public')), "
            "CHECK ((requester_kind = 'public') = (requester_id IS NULL)))"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO artifact_fetch_audit VALUES ('miner', 'owner-hotkey')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.exec_driver_sql(
            "SELECT requester_kind, requester_id FROM artifact_fetch_audit"
        ).one() == ("miner", "owner-hotkey")
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "INSERT INTO artifact_fetch_audit VALUES ('miner', 'another-owner')"
            )
        connection.exec_driver_sql(
            "INSERT INTO artifact_fetch_audit VALUES ('validator', 'validator-hotkey')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "INSERT INTO artifact_fetch_audit VALUES ('miner', NULL)"
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "INSERT INTO artifact_fetch_audit VALUES ('unknown', 'hotkey')"
            )

    async with engine.connect() as connection:
        await connection.run_sync(probe)
        await connection.rollback()

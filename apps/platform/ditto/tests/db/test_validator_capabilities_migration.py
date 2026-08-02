"""Upgrade/downgrade proof for validator capability heartbeat storage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_validator_capabilities_migration_round_trip() -> None:
    migration_path = (
        Path(__file__).parents[3]
        / "alembic/versions/2026_07_18_add_validator_capability_heartbeats.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validator_cap_migration", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = cast(Any, importlib.util.module_from_spec(spec))
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "validator_heartbeats",
        metadata,
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("validator_heartbeats")
        }
        assert upgraded == {"validator_hotkey", "capabilities", "stack"}

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("validator_heartbeats")
        }
        assert downgraded == {"validator_hotkey"}

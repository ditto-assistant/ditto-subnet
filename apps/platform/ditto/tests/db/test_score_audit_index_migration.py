"""Recover actual concurrent-build INVALID leftovers, including in one run."""

from __future__ import annotations

import asyncio
import logging
from types import ModuleType

import asyncpg
import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ditto.tests import pgharness
from ditto.tests.pgharness import WorkerDatabase

INDEX = "score_audit_log_event_agent_idx"
CREATE = f"CREATE INDEX CONCURRENTLY {INDEX} ON score_audit_log (event, agent_id)"


def _migration() -> ModuleType:
    script = ScriptDirectory.from_config(pgharness._alembic_config()).get_revision(
        "b4c9f7e2a810"
    )
    assert script is not None
    return script.module


def _upgrade(connection: Connection, migration: ModuleType) -> None:
    with Operations.context(MigrationContext.configure(connection=connection)):
        migration.upgrade()


async def _valid(engine: AsyncEngine) -> bool | None:
    async with engine.connect() as conn:
        return await conn.scalar(
            text(
                "SELECT i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE c.relname=:name"
            ),
            {"name": INDEX},
        )


@pytest.mark.parametrize("leftover", [False, True])
async def test_upgrade_recovers_cancelled_build_and_preserves_valid_index(
    engine: AsyncEngine, worker_database: WorkerDatabase, leftover: bool
) -> None:
    if leftover:
        builder = await asyncpg.connect(
            worker_database.dsn.asyncpg, server_settings={"lock_timeout": "100ms"}
        )
        blocker = await asyncpg.connect(worker_database.dsn.asyncpg)
        try:
            await builder.execute(f"DROP INDEX {INDEX}")
            await blocker.execute(
                "BEGIN; LOCK TABLE score_audit_log IN ROW EXCLUSIVE MODE"
            )
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await builder.execute(CREATE)
            assert await _valid(engine) is False
            await blocker.execute("ROLLBACK")
        finally:
            await blocker.close()
            await builder.close()
    async with engine.connect() as conn:
        before = await conn.scalar(
            text("SELECT oid FROM pg_class WHERE relname=:name"), {"name": INDEX}
        )
        await conn.run_sync(_upgrade, _migration())
    assert await _valid(engine) is True
    async with engine.connect() as conn:
        after = await conn.scalar(
            text("SELECT oid FROM pg_class WHERE relname=:name"), {"name": INDEX}
        )
    if not leftover:
        assert before == after  # Already valid: no destructive rebuild.


async def test_upgrade_recovers_in_same_run_after_writer_timeout(
    engine: AsyncEngine,
    worker_database: WorkerDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    migration = _migration()
    monkeypatch.setattr(migration, "backoff_delay", lambda _: 0.03)
    blocker = await asyncpg.connect(worker_database.dsn.asyncpg)
    await blocker.execute(f"DROP INDEX {INDEX}")
    await blocker.execute("BEGIN; LOCK TABLE score_audit_log IN ROW EXCLUSIVE MODE")
    release = asyncio.create_task(blocker.execute("SELECT pg_sleep(0.6); ROLLBACK"))
    runner = create_async_engine(
        worker_database.dsn.sqlalchemy,
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "100ms"}},
    )
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.lock"):
            async with runner.connect() as conn:
                await conn.run_sync(_upgrade, migration)
                assert await conn.scalar(text("SHOW lock_timeout")) == "100ms"
        await release
        assert any("55P03" in record.getMessage() for record in caplog.records)
        assert any("INVALID" in record.getMessage() for record in caplog.records)
        assert await _valid(engine) is True
    finally:
        if not release.done():
            release.cancel()
        await asyncio.gather(release, return_exceptions=True)
        await blocker.close()
        await runner.dispose()

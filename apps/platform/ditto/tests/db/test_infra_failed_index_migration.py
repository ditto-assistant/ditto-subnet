"""The infra-failure partial index follows the retried reason codes (#2444, #2449).

Each upgrade must rebuild ``screening_attempts_infra_failed_idx`` over exactly the
retried codes; each downgrade must restore the prior predicate. Every direction
leaves one valid index and no temporary build behind.

The #2449 rebuild must also recover from what an interrupted concurrent build
leaves: a ``lock_timeout`` during the build's waits cancels it and leaves an
INVALID index that ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` would keep. It
must recover within the run that hit the timeout, and on a re-run from any
invalid or stale index under either name. Only its CONCURRENTLY statements run
under the wider ``lock_timeout``; the session keeps env.py's short bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from collections.abc import AsyncIterator
from types import ModuleType

import asyncpg
import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ditto.db.queries.screening_infra_retry import INFRA_AUTO_RETRY_REASON_CODES
from ditto.tests import pgharness
from ditto.tests.pgharness import WorkerDatabase

_INDEX = "screening_attempts_infra_failed_idx"
_BUILD = "screening_attempts_infra_failed_swap_idx"
_KEY_REVISION = "6a7a2a03a65f"
_KEY_CODE = "source-review-adjudicator-key-unavailable"
_PRIOR_CODES = (
    "docker-build-infrastructure",
    "worker-claim-not-started",
    "l2-runtime-evidence-unavailable",
)

# (widening revision, the code it added, the codes it kept). Each case downgrades
# to that revision's own parent as the script declares it, so a rebase that
# repoints ``down_revision`` keeps testing exactly one widening.
_WIDENINGS = (
    (
        "5e2a8c4f9d17",
        "l2-runtime-evidence-unavailable",
        ("docker-build-infrastructure", "worker-claim-not-started"),
    ),
    (_KEY_REVISION, _KEY_CODE, _PRIOR_CODES),
)


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


def _script(revision: str) -> Script:
    script = ScriptDirectory.from_config(pgharness._alembic_config()).get_revision(
        revision
    )
    assert script is not None
    return script


def _parent(revision: str) -> str:
    parent = _script(revision).down_revision
    assert isinstance(parent, str)
    return parent


def _predicate(codes: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{code}'" for code in codes)
    return f"status = 'failed' AND reason_code IN ({quoted})"


async def _indexes(engine: AsyncEngine) -> dict[str, tuple[bool, str]]:
    """The target and temporary build indexes: name -> (valid, definition)."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname IN (:index, :build)"
            ),
            {"index": _INDEX, "build": _BUILD},
        )
        return {row[0]: (bool(row[1]), str(row[2])) for row in rows}


def _assert_only_target(
    indexes: dict[str, tuple[bool, str]], codes: tuple[str, ...]
) -> None:
    """One valid index on ``finished_at`` over exactly ``codes``, no leftover."""
    assert set(indexes) == {_INDEX}, indexes
    valid, definition = indexes[_INDEX]
    assert valid, definition
    assert (
        " ON public.screening_attempts USING btree (finished_at) "
        "WHERE ((status = 'failed'::text) AND (reason_code = ANY (ARRAY["
    ) in definition, definition
    named = re.findall(r"'([a-z0-9-]+)'::text", definition)
    assert sorted(named) == sorted(("failed", *codes)), definition


@pytest.mark.parametrize(("revision", "added", "kept"), _WIDENINGS)
async def test_infra_failed_index_round_trip(
    engine: AsyncEngine, revision: str, added: str, kept: tuple[str, ...]
) -> None:
    assert added not in kept
    try:
        _assert_only_target(await _indexes(engine), INFRA_AUTO_RETRY_REASON_CODES)

        _alembic("downgrade", _parent(revision))
        _assert_only_target(await _indexes(engine), kept)

        _alembic("upgrade", "head")
        _assert_only_target(await _indexes(engine), INFRA_AUTO_RETRY_REASON_CODES)
    finally:
        # Keep this worker database usable even when an assertion above fails.
        _alembic("upgrade", "head")


async def _leave_invalid_build(database: WorkerDatabase, statement: str) -> None:
    """Run a concurrent build the way a deploy loses one to ``lock_timeout``.

    An open writer holds ``ROW EXCLUSIVE``, so the build's wait for older
    writers times out after its catalog entry committed: the index stays behind
    INVALID, exactly as when the migration's own build is cancelled.
    """
    blocker = await asyncpg.connect(database.dsn.asyncpg)
    builder = await asyncpg.connect(
        database.dsn.asyncpg, server_settings={"lock_timeout": "100ms"}
    )
    try:
        await blocker.execute(
            "BEGIN; LOCK TABLE screening_attempts IN ROW EXCLUSIVE MODE"
        )
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await builder.execute(statement)
        await blocker.execute("ROLLBACK")
    finally:
        await builder.close()
        await blocker.close()


_UPGRADED = (*_PRIOR_CODES, _KEY_CODE)
# What an earlier, interrupted, or hand-edited run can leave under either name.
# The upgrade must replace every one of them, never swap it in or keep it.
_LEFTOVERS = {
    "invalid-build": (
        f"CREATE INDEX CONCURRENTLY {_BUILD} ON screening_attempts (finished_at) "
        f"WHERE {_predicate(_UPGRADED)}"
    ),
    "valid-build-on-another-column": (
        f"CREATE INDEX {_BUILD} ON screening_attempts (started_at) "
        f"WHERE {_predicate(_UPGRADED)}"
    ),
    "valid-build-with-an-extra-code": (
        f"CREATE INDEX {_BUILD} ON screening_attempts (finished_at) "
        f"WHERE {_predicate((*_UPGRADED, 'retired-code'))}"
    ),
    "target-with-an-extra-code": (
        f"DROP INDEX {_INDEX}; CREATE INDEX {_INDEX} ON screening_attempts "
        f"(finished_at) WHERE {_predicate((*_UPGRADED, 'retired-code'))}"
    ),
    "target-on-another-column": (
        f"DROP INDEX {_INDEX}; CREATE INDEX {_INDEX} ON screening_attempts "
        f"(started_at) WHERE {_predicate(_UPGRADED)}"
    ),
}


@pytest.mark.parametrize("leftover", sorted(_LEFTOVERS))
async def test_key_code_rebuild_replaces_an_invalid_or_stale_leftover(
    engine: AsyncEngine, worker_database: WorkerDatabase, leftover: str
) -> None:
    statement = _LEFTOVERS[leftover]
    try:
        _alembic("downgrade", _parent(_KEY_REVISION))
        if leftover == "invalid-build":
            await _leave_invalid_build(worker_database, statement)
            valid, _ = (await _indexes(engine))[_BUILD]
            assert not valid
        else:
            async with engine.begin() as connection:
                for part in statement.split("; "):
                    await connection.exec_driver_sql(part)

        _alembic("upgrade", "head")

        _assert_only_target(await _indexes(engine), INFRA_AUTO_RETRY_REASON_CODES)
    finally:
        _alembic("upgrade", "head")


def _upgrade_in_process(connection: Connection, migration: ModuleType) -> None:
    with Operations.context(MigrationContext.configure(connection=connection)):
        migration.upgrade()


@contextlib.asynccontextmanager
async def _writer_holding_the_table(
    database: WorkerDatabase, *, seconds: float
) -> AsyncIterator[None]:
    """Hold ``ROW EXCLUSIVE`` on ``screening_attempts`` for ``seconds``.

    The server ends the writer itself (``pg_sleep``, then ``ROLLBACK``),
    whatever the migration's backoff does to this event loop meanwhile. The
    release is settled before the connection closes: awaited when the body
    completes, cancelled and awaited when it raises, so a failing migration
    reports its own error instead of an orphaned query task.
    """
    blocker = await asyncpg.connect(database.dsn.asyncpg)
    try:
        await blocker.execute(
            "BEGIN; LOCK TABLE screening_attempts IN ROW EXCLUSIVE MODE"
        )
        released = asyncio.create_task(
            blocker.execute(f"SELECT pg_sleep({seconds}); ROLLBACK")
        )
        try:
            yield
        except BaseException:
            released.cancel()
            await asyncio.wait({released})
            if not released.cancelled():
                # Retrieved and dropped: the body's error is the one to report.
                released.exception()
            raise
        await released
    finally:
        await blocker.close()


async def test_key_code_rebuild_recovers_in_run_from_a_cancelled_build(
    engine: AsyncEngine,
    worker_database: WorkerDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A build cancelled by ``lock_timeout`` is rebuilt, not skipped, in one run.

    A writer holds the table past the build's (shortened) lock timeout, so the
    first ``CREATE INDEX CONCURRENTLY`` is cancelled and leaves an INVALID
    index, then the writer finishes. The same run must drop that leftover and
    build again instead of retrying ``IF NOT EXISTS`` into a RuntimeError.
    """
    migration = _script(_KEY_REVISION).module
    monkeypatch.setattr(migration, "_CONCURRENT_LOCK_TIMEOUT", "150ms")
    try:
        _alembic("downgrade", _parent(_KEY_REVISION))
        # env.py sets lock_timeout as a startup parameter; mirror it, shorter.
        runner = create_async_engine(
            worker_database.dsn.sqlalchemy,
            poolclass=NullPool,
            connect_args={"server_settings": {"lock_timeout": "100ms"}},
        )
        try:
            with caplog.at_level(logging.WARNING, logger="alembic.lock"):
                async with (
                    _writer_holding_the_table(worker_database, seconds=0.6),
                    runner.connect() as connection,
                ):
                    await connection.run_sync(_upgrade_in_process, migration)
        finally:
            await runner.dispose()

        # The first build really was cancelled, so the recovery path ran.
        assert any("55P03" in record.getMessage() for record in caplog.records)
        _assert_only_target(await _indexes(engine), INFRA_AUTO_RETRY_REASON_CODES)
    finally:
        _alembic("upgrade", "head")


async def test_key_code_rebuild_widens_the_lock_timeout_only_for_its_build(
    engine: AsyncEngine,
    worker_database: WorkerDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The build outwaits a writer the session bound would cancel, then resets it.

    env.py's short ``lock_timeout`` is a startup parameter that every later
    migration in the batch relies on. Only the CONCURRENTLY statements may run
    under the wider bound, so a writer that outlives the session default must
    not cancel the build, and afterwards the session must be back on its
    startup bound rather than keep the wide one.
    """
    migration = _script(_KEY_REVISION).module
    monkeypatch.setattr(migration, "_CONCURRENT_LOCK_TIMEOUT", "10s")
    try:
        _alembic("downgrade", _parent(_KEY_REVISION))
        runner = create_async_engine(
            worker_database.dsn.sqlalchemy,
            poolclass=NullPool,
            connect_args={"server_settings": {"lock_timeout": "100ms"}},
        )
        try:
            with caplog.at_level(logging.WARNING, logger="alembic.lock"):
                # Held six times the session bound, well inside the widened one.
                async with (
                    _writer_holding_the_table(worker_database, seconds=0.6),
                    runner.connect() as connection,
                ):
                    await connection.run_sync(_upgrade_in_process, migration)
                    after = await connection.scalar(text("SHOW lock_timeout"))
        finally:
            await runner.dispose()

        assert not any("55P03" in record.getMessage() for record in caplog.records)
        assert after == "100ms"
        _assert_only_target(await _indexes(engine), INFRA_AUTO_RETRY_REASON_CODES)
    finally:
        _alembic("upgrade", "head")

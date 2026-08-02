"""Contract tests for the migration lock helpers.

The property that matters most here is **idempotency**. ``alembic/env.py``
retries a lock-contended migration, and a replay re-runs whatever did not
commit; since these helpers run in an ``autocommit_block``, some of their work
has committed by then. If :func:`safe_add_column` were not re-runnable that
retry would corrupt a deploy rather than rescue one, so the replay cases below
are the load-bearing tests, not the happy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from ditto.db.migration_lock import (
    RETRYABLE_SQLSTATES,
    backoff_delay,
    is_retryable,
    safe_add_column,
    safe_drop_column,
    sqlstate,
)

# ─── Pure helpers ────────────────────────────────────────────────────────────


class _Orig:
    def __init__(self, sqlstate: str | None = None, pgcode: str | None = None) -> None:
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode


class _Wrapped(Exception):
    def __init__(self, orig: _Orig) -> None:
        super().__init__("wrapped")
        self.orig = orig


def test_sqlstate_reads_asyncpg_and_psycopg_spellings() -> None:
    assert sqlstate(_Wrapped(_Orig(sqlstate="55P03"))) == "55P03"
    assert sqlstate(_Wrapped(_Orig(pgcode="40P01"))) == "40P01"
    assert sqlstate(Exception("no driver info")) is None


def test_only_lock_contention_is_retryable() -> None:
    assert set(RETRYABLE_SQLSTATES) == {"55P03", "40P01"}
    assert is_retryable(_Wrapped(_Orig(sqlstate="55P03")))
    assert is_retryable(_Wrapped(_Orig(sqlstate="40P01")))
    # A real failure must fail the deploy, not spin. 23505 is unique_violation.
    assert not is_retryable(_Wrapped(_Orig(sqlstate="23505")))
    assert not is_retryable(Exception("boom"))


def test_backoff_is_jittered_bounded_and_nondecreasing() -> None:
    for attempt in range(1, 13):
        base = min(0.5 * (2 ** (attempt - 1)), 8.0)
        samples = [backoff_delay(attempt) for _ in range(50)]
        assert all(base * 0.5 <= s <= base * 1.5 for s in samples)
    # Capped, so a long retry budget cannot turn into an unbounded deploy hang.
    assert max(backoff_delay(50) for _ in range(50)) <= 12.0
    # Jittered, so retries from separate deploys do not land on the same crest.
    assert len({round(backoff_delay(5), 6) for _ in range(20)}) > 1


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("inference requests", "ok"),
        ("ok", "DROP TABLE x"),
        ("ok", "col;--"),
        ("Ok", "col"),
        ("", "col"),
    ],
)
def test_non_identifiers_are_rejected(table: str, column: str) -> None:
    with pytest.raises(ValueError, match="bare lowercase identifier"):
        safe_add_column(table, column, "BIGINT")
    with pytest.raises(ValueError, match="bare lowercase identifier"):
        safe_drop_column(table, column)


# ─── Against real Postgres ───────────────────────────────────────────────────

PROBE = "migration_lock_probe"


@contextmanager
def _operations(connection: Connection) -> Iterator[None]:
    """Install the ``op`` proxy over *connection*, transaction-clean.

    The helpers run in an ``autocommit_block``, which asserts that any open
    transaction belongs to the migration context. A preceding read in the same
    test will have autobegun one on the connection, so clear it first. Under
    ``alembic upgrade`` this does not arise -- the context opens the
    transaction itself.
    """
    connection.rollback()
    with Operations.context(MigrationContext.configure(connection)):
        yield


def _fresh(connection: Connection, rows: int) -> None:
    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {PROBE}")
    connection.exec_driver_sql(
        f"CREATE TABLE {PROBE} (id bigserial PRIMARY KEY, src bigint NOT NULL)"
    )
    connection.exec_driver_sql(
        f"INSERT INTO {PROBE} (src) SELECT g FROM generate_series(1, {rows}) g"
    )
    connection.commit()


def _state(connection: Connection, column: str) -> dict[str, object]:
    row = connection.exec_driver_sql(
        "SELECT attnotnull, atthasmissing FROM pg_attribute "
        f"WHERE attrelid = '{PROBE}'::regclass AND attname = '{column}'"
    ).first()
    nulls = connection.exec_driver_sql(
        f"SELECT count(*) FROM {PROBE} WHERE {column} IS NULL"
    ).scalar()
    mismatched = connection.exec_driver_sql(
        f"SELECT count(*) FROM {PROBE} WHERE {column} IS DISTINCT FROM src"
    ).scalar()
    default = connection.exec_driver_sql(
        f"SELECT column_default FROM information_schema.columns "
        f"WHERE table_name = '{PROBE}' AND column_name = '{column}'"
    ).scalar()
    return {
        "not_null": row is not None and row[0],
        "has_missing": row is not None and row[1],
        "nulls": nulls,
        "mismatched": mismatched,
        "default": default,
    }


def _backfilled_add(connection: Connection) -> None:
    """The three-phase shape: add nullable, backfill in batches, then pin."""
    with _operations(connection):
        safe_add_column(
            PROBE,
            "copied",
            "BIGINT",
            backfill="src",
            not_null=True,
            server_default="0",
            # Force several batches out of a small table, so the ctid page walk
            # and the convergence sweep are both actually exercised.
            batch_pages=1,
        )


async def test_safe_add_column_backfills_and_pins(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.run_sync(_fresh, 2000)
        await conn.run_sync(_backfilled_add)
        state = await conn.run_sync(_state, "copied")

    assert state["nulls"] == 0
    assert state["mismatched"] == 0
    assert state["not_null"] is True
    assert state["default"] is not None


async def test_safe_add_column_is_replayable(engine: AsyncEngine) -> None:
    """Replaying converges instead of raising -- what the env.py retry needs."""

    def _half_done(connection: Connection) -> None:
        connection.exec_driver_sql(f"ALTER TABLE {PROBE} ADD COLUMN copied BIGINT")
        connection.exec_driver_sql(f"UPDATE {PROBE} SET copied = src WHERE id <= 500")
        connection.commit()

    async with engine.connect() as conn:
        await conn.run_sync(_fresh, 2000)
        # Die part-way: column exists, backfill half done, no constraint yet.
        await conn.run_sync(_half_done)
        await conn.run_sync(_backfilled_add)
        first = await conn.run_sync(_state, "copied")
        # And again over a fully-completed run.
        await conn.run_sync(_backfilled_add)
        second = await conn.run_sync(_state, "copied")

    for state in (first, second):
        assert state["nulls"] == 0
        assert state["mismatched"] == 0
        assert state["not_null"] is True


async def test_defaulted_add_never_rewrites_the_heap(engine: AsyncEngine) -> None:
    """The one-shot path is metadata-only on PostgreSQL 11+.

    A non-volatile default is stored in ``pg_attribute.attmissingval`` rather
    than written into every row, so ``NOT NULL DEFAULT`` does not rewrite the
    table and lock acquisition is the only slow part. ``atthasmissing`` is the
    observable proof; the heap size not moving is the consequence.
    """

    def _one_shot(connection: Connection) -> None:
        with _operations(connection):
            safe_add_column(
                PROBE, "version", "INTEGER", not_null=True, server_default="1"
            )

    def _size(connection: Connection) -> int:
        return int(
            connection.exec_driver_sql(f"SELECT pg_relation_size('{PROBE}')").scalar()
            or 0
        )

    async with engine.connect() as conn:
        await conn.run_sync(_fresh, 2000)
        before = await conn.run_sync(_size)
        await conn.run_sync(_one_shot)
        after = await conn.run_sync(_size)
        state = await conn.run_sync(_state, "version")

    assert after == before, "heap was rewritten; the default must be non-volatile"
    assert state["has_missing"] is True
    assert state["not_null"] is True
    assert state["nulls"] == 0


async def test_safe_drop_column_round_trips(engine: AsyncEngine) -> None:
    def _drop(connection: Connection) -> None:
        with _operations(connection):
            safe_drop_column(PROBE, "copied")

    def _has_copied(connection: Connection) -> bool:
        return bool(
            connection.exec_driver_sql(
                "SELECT count(*) FROM information_schema.columns "
                f"WHERE table_name = '{PROBE}' AND column_name = 'copied'"
            ).scalar()
        )

    async with engine.connect() as conn:
        await conn.run_sync(_fresh, 100)
        await conn.run_sync(_backfilled_add)
        assert await conn.run_sync(_has_copied) is True
        await conn.run_sync(_drop)
        assert await conn.run_sync(_has_copied) is False
        # Idempotent: dropping an absent column is a no-op, not an error.
        await conn.run_sync(_drop)
        assert await conn.run_sync(_has_copied) is False

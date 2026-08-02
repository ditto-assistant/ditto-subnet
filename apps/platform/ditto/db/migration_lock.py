"""Lock-safe primitives for Alembic migrations against hot tables.

This is the reusable form of the fix in ditto-platform#481, where migration
``b7e41c93a05d`` deadlocked against live traffic twice and blocked a deploy. The
incident is worth restating, because every rule here follows from it.

That migration ran four statements in one transaction: ``ADD COLUMN`` on
``inference_requests``, a whole-table backfill, ``SET NOT NULL``, then ``ADD
COLUMN`` on ``inference_grants``. Two faults combined:

*Lock duration.* ``ADD COLUMN`` takes an ``AccessExclusiveLock`` held until
commit, so the exclusive lock on ``inference_requests`` was held across a
472k-row backfill. Every inference on the platform stalled behind it.

*Lock order.* ``begin_inference_request`` locks ``validator_tickets``, then
``inference_grants``, then ``inference_requests``. The migration took them in
the reverse order, so the callers stalled behind the backfill were holding a
``RowShareLock`` on ``inference_grants`` at exactly the moment the migration
asked for it. That is a genuine cycle, not lock-wait, which is why a retry loop
bolted onto the old shape does not converge: the migration's own long lock
manufactures the population that blocks it.

The shape that is safe, and what :func:`safe_add_column` emits for you:

1. add the column **nullable and undefaulted**, which is metadata-only;
2. backfill in small committed batches under a ``RowExclusiveLock``, which
   coexists with the row locks the application path takes;
3. take one short explicit ``ACCESS EXCLUSIVE`` window to sweep the residue and
   pin the constraint, bounded by ``lock_timeout`` and retried with backoff.

Two properties are load-bearing throughout.

**Every step is idempotent** (``IF NOT EXISTS``, ``WHERE ... IS NULL``, a ``SET
NOT NULL`` that no-ops when already set). Steps run in an ``autocommit_block``,
so a run that dies between batches has committed some of its work; idempotency
is what makes re-running it safe. :file:`alembic/env.py` retries a
lock-contended migration, and that retry is only sound because of this.

**Nothing here knows anything about the schema.** Migrations importing
application code is normally an anti-pattern -- the app moves on and old
migrations break. This module is exempt by construction: it imports no models,
no metadata and no config, and its entire vocabulary is table and column names
passed in as strings. Keep it that way, or the exemption stops holding.
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import time
from collections.abc import Sequence

from sqlalchemy import Connection, exc

from alembic import op

log = logging.getLogger("alembic.lock")


LOCK_TIMEOUT = "3s"
"""How long one statement waits for a table lock before giving up.

Short on purpose. A statement queued for an exclusive lock also blocks every
request queuing behind it, so a long wait is itself an outage. The retry is
what makes a short timeout safe.

This bounds lock *acquisition* only, never statement runtime, so it cannot
truncate a long backfill.
"""

MAX_ATTEMPTS = 12
"""Attempts per lock-taking statement, ~0.5s to ~8s of backoff between them."""

BACKFILL_PAGES = 2000
"""Heap pages per backfill batch, roughly 16MB at the default 8kB page size.
Small enough that one batch is a short write transaction, large enough that a
262MB table is a couple of dozen batches rather than thousands."""

SWEEP_ROWS = 20000
"""Rows per convergence-sweep batch, for rows appended past the end of the heap
while the page walk was running."""

RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01"})
"""``lock_not_available`` and ``deadlock_detected``.

``55P03`` is our own ``lock_timeout`` firing, which is the *designed* outcome
under contention. ``40P01`` is included because ``lock_timeout`` does not
prevent deadlocks and was never going to: the deadlock detector runs at
``deadlock_timeout`` (1s by default), so on a genuine cycle it fires first and
the waiter gets ``40P01`` well before a 3s ``lock_timeout`` would have expired.
Verified empirically, not assumed. Retrying is what turns that into a bounded
backoff instead of a failed deploy.
"""

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(value: str, kind: str) -> str:
    """Reject anything that is not a bare lowercase SQL identifier.

    Every caller is a migration author writing a literal, so this is a typo
    guard rather than an injection guard -- but it is also what lets the rest
    of this module interpolate names into SQL without ceremony.
    """
    if not _IDENT.match(value):
        raise ValueError(f"{kind} {value!r} is not a bare lowercase identifier")
    return value


def sqlstate(error: BaseException) -> str | None:
    """Pull the SQLSTATE off a driver error, whichever driver raised it."""
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlstate", None)
    if code is None:
        code = getattr(original, "pgcode", None)
    return str(code) if code is not None else None


def is_retryable(error: BaseException) -> bool:
    """True when *error* is lock contention rather than a real failure."""
    return sqlstate(error) in RETRYABLE_SQLSTATES


def backoff_delay(attempt: int) -> float:
    """Exponential backoff, capped at 8s, with jitter.

    Jitter matters: three validators at three to four slots each produce bursty
    load, and an unjittered retry lands on the same crest every time.
    """
    return min(0.5 * (2 ** (attempt - 1)), 8.0) * (0.5 + random.random())


def run_with_retry(
    bind: Connection,
    statements: Sequence[str],
    what: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> None:
    """Run *statements* as one transaction, retrying on lock contention.

    Call from inside an ``autocommit_block``: the transaction is opened with
    explicit SQL rather than by SQLAlchemy, because the point is to control
    exactly what commits when. On a retryable failure the whole transaction is
    rolled back -- releasing every lock it held, which is what lets the
    competing traffic drain -- and replayed after a backoff.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            bind.exec_driver_sql("BEGIN")
            bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            for statement in statements:
                bind.exec_driver_sql(statement)
            bind.exec_driver_sql("COMMIT")
            return
        except exc.DBAPIError as error:
            # Best-effort: a ROLLBACK that itself fails must not replace the
            # error that actually explains the failure. A deploy log naming the
            # wrong cause is worse than one naming none.
            with contextlib.suppress(exc.DBAPIError):
                bind.exec_driver_sql("ROLLBACK")
            if not is_retryable(error) or attempt == max_attempts:
                raise
            delay = backoff_delay(attempt)
            log.warning(
                "%s: %s on attempt %d/%d; retrying in %.1fs",
                what,
                sqlstate(error),
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)


def _backfill_column(
    bind: Connection,
    table: str,
    column: str,
    expression: str,
    batch_pages: int,
) -> None:
    """Fill *column* from *expression* batch by batch, never holding the table.

    Walks the heap in ``ctid`` page ranges rather than repeatedly re-scanning
    for ``IS NULL`` with a ``LIMIT``: the range form is bounded work per batch
    and linear overall, where the ``LIMIT`` form rescans the already-backfilled
    prefix on every pass and degrades quadratically.

    The page count is read once, up front. Rows appended after that -- the old
    build is still serving throughout -- fall outside the walk, so a
    convergence sweep follows, and whatever is still outstanding after that is
    caught by the residue update in :func:`_finalize_column`, under the lock
    that makes it exhaustive.
    """
    pages = bind.exec_driver_sql(
        f"SELECT pg_relation_size('{table}') / current_setting('block_size')::bigint"
    ).scalar()
    total = int(pages or 0) + 1

    for start in range(0, total, batch_pages):
        end = start + batch_pages
        run_with_retry(
            bind,
            [
                f"UPDATE {table} SET {column} = {expression} "
                f"WHERE {column} IS NULL "
                f"AND ctid >= '({start},0)'::tid AND ctid < '({end},0)'::tid"
            ],
            f"{table}.{column} backfill pages {start}-{end}",
        )

    # Rows appended past the original end of the heap while the walk ran.
    # Bounded: each pass takes the oldest outstanding batch, and the insert
    # rate is far below the rate this drains at.
    for _ in range(MAX_ATTEMPTS):
        remaining = bind.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {column} IS NULL"
        ).scalar()
        if not remaining:
            return
        run_with_retry(
            bind,
            [
                f"UPDATE {table} SET {column} = {expression} WHERE ctid IN ("
                f"  SELECT ctid FROM {table} WHERE {column} IS NULL "
                f"  LIMIT {SWEEP_ROWS})"
            ],
            f"{table}.{column} backfill sweep",
        )


def _finalize_column(
    bind: Connection,
    table: str,
    column: str,
    residue: str | None,
    not_null: bool,
    server_default: str | None,
) -> None:
    """Sweep the residue and pin the constraint in one short exclusive window.

    The explicit ``LOCK TABLE`` is what makes the residue update and ``SET NOT
    NULL`` atomic with respect to inserts: do them in separate transactions and
    a row landing in between fails the constraint. ``SET NOT NULL`` still scans
    the table, but it scans a table it already holds, so this is a sub-second
    stall rather than a rewrite -- and it is bounded by ``lock_timeout`` and
    retried, so acquisition degrades into a retry instead of a deadlock.
    """
    statements = [f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"]
    if residue is not None:
        statements.append(
            f"UPDATE {table} SET {column} = {residue} WHERE {column} IS NULL"
        )

    clauses = []
    if not_null:
        clauses.append(f"ALTER COLUMN {column} SET NOT NULL")
    if server_default is not None:
        clauses.append(f"ALTER COLUMN {column} SET DEFAULT {server_default}")
    statements.append(f"ALTER TABLE {table} " + ", ".join(clauses))

    run_with_retry(bind, statements, f"{table}.{column} finalize")


def safe_add_column(
    table: str,
    column: str,
    type_: str,
    *,
    backfill: str | None = None,
    not_null: bool = False,
    server_default: str | None = None,
    batch_pages: int = BACKFILL_PAGES,
) -> None:
    """Add a column to a hot table without holding it exclusively.

    Use this instead of :func:`alembic.op.add_column` on any table the request
    path writes to. Raw SQL rather than ``op.add_column`` because the shape
    needs ``IF NOT EXISTS``, which the Alembic operation cannot emit and which
    is what makes a retried migration idempotent.

    *type_*, *backfill* and *server_default* are SQL text, not identifiers, and
    are interpolated verbatim -- they are migration-author literals. *table* and
    *column* are checked as bare identifiers.

    :param backfill: SQL expression evaluated per row to populate existing rows
        (e.g. another column name). Omit for a column with no historical value.
    :param not_null: pin ``NOT NULL`` once every row has a value.
    :param server_default: the ``DEFAULT`` to leave on the column.

    Two shapes come out of this, matching what ditto-platform#481 landed by
    hand:

    **One-shot**, when there is no backfill but there is a ``NOT NULL DEFAULT``.
    PostgreSQL 11+ stores a non-volatile default in ``pg_attribute.attmissingval``
    instead of rewriting the heap, so this never rewrites the table and lock
    acquisition is the only slow part. Volatile defaults (``now()``,
    ``random()``, ``gen_random_uuid()``) *do* rewrite -- pass one as *backfill*
    instead.

    **Three-phase**, whenever there is a backfill: add nullable and undefaulted,
    backfill in batches, then pin the constraint in one short window. The
    default is deliberately withheld until that last step. Set earlier, rows
    written by the still-running old build during the backfill would silently
    carry it, which in #481 would have meant a zero charge ceiling and a 409 on
    successful calls. Leaving the column nullable until the end is inert: the
    old build does not know the column exists.

    Runs outside the migration's transaction, so do not call it inside an
    ``autocommit_block`` you opened yourself.
    """
    _ident(table, "table")
    _ident(column, "column")

    bind = op.get_bind()
    with op.get_context().autocommit_block():
        if backfill is None and not_null and server_default is not None:
            run_with_retry(
                bind,
                [
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                    f"{column} {type_} NOT NULL DEFAULT {server_default}"
                ],
                f"add {table}.{column}",
            )
            return

        run_with_retry(
            bind,
            [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_}"],
            f"add {table}.{column}",
        )

        if backfill is not None:
            _backfill_column(bind, table, column, backfill, batch_pages)

        if not_null or server_default is not None:
            # `backfill or server_default`: whichever value existing rows are
            # supposed to end up with. If neither was given and NOT NULL was
            # asked for, there is nothing to fill residue with and SET NOT NULL
            # will fail on any pre-existing row -- correctly, since the author
            # did not say what those rows should hold.
            _finalize_column(
                bind,
                table,
                column,
                backfill or server_default,
                not_null,
                server_default,
            )


def safe_drop_column(table: str, column: str) -> None:
    """Drop a column from a hot table, bounded and retried.

    ``DROP COLUMN`` is metadata-only in PostgreSQL -- the heap is not rewritten
    -- so acquisition is the only hazard, and that is what the retry covers.
    Idempotent via ``IF EXISTS``.

    One column per call, in its own transaction. A single transaction dropping
    columns from two hot tables is exactly the two-table exclusive lock that
    deadlocked in #481.
    """
    _ident(table, "table")
    _ident(column, "column")

    bind = op.get_bind()
    with op.get_context().autocommit_block():
        run_with_retry(
            bind,
            [f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"],
            f"drop {table}.{column}",
        )

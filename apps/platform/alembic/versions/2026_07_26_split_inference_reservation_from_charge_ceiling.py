"""split the inference reservation from its charge ceiling, and version the meter

``inference_requests.reserved_tokens`` was doing two incompatible jobs. It was
the amount held against a grant's token allowance while a request was in
flight, AND the ceiling that untrusted provider accounting was clamped to. Both
worked only because the value was ``max_tokens + len(body)`` -- byte length used
directly as a token count, a genuine upper bound but roughly 4x the truth.

Making the reservation an honest estimate breaks the second job: a legitimate
token-dense prompt lands a little above the estimate, and
``finish_inference_request`` marks anything over its bound non-deliverable,
which would 409 ordinary successful calls back to the harness. So the ceiling
becomes its own column and keeps the byte-derived definition.

``max_chargeable_tokens`` backfills from ``reserved_tokens``, which is exactly
right for historical rows: under the old contract that value *was* this bound.

``inference_grants.usage_accounting_version`` records which meter booked a
grant's counters, on the same principle as ``bench_version`` for scores -- a
token total is only comparable within the contract that produced it. Existing
rows are version 1 (server default); new grants are stamped 2 by the
application. There is deliberately no backfill of the counters themselves: the
over-charge happened at reservation time, so what those calls actually consumed
was never recorded and cannot be recovered.

Additive and non-destructive. No existing counter is rewritten.

Lock safety
-----------

The first version of this migration deadlocked against live traffic twice and
blocked the deploy (relation 23343 = ``inference_requests``, 23317 =
``inference_grants``). It ran four statements in one transaction:
``ADD COLUMN`` on ``inference_requests``, a whole-table backfill, ``SET NOT
NULL``, then ``ADD COLUMN`` on ``inference_grants``. Two separate faults
combined.

*Lock duration.* ``ADD COLUMN`` takes an ``AccessExclusiveLock`` that is held
until the transaction commits, so the exclusive lock on ``inference_requests``
was held across a 472k-row, 262MB backfill. Every inference on the platform
stalled behind it for the whole rewrite.

*Lock order.* ``begin_inference_request`` locks ``validator_tickets``, then
``inference_grants``, then ``inference_requests``. The migration took
``inference_requests`` first and ``inference_grants`` last -- the exact reverse.
The stalled callers were holding a ``RowShareLock`` on ``inference_grants``
while waiting on ``inference_requests``, so the moment the migration asked for
``inference_grants`` the cycle closed. That is not a race a retry wins: the
migration's own long lock is what manufactures the population that blocks it.

So the fix is structural, not a retry loop bolted onto the old shape:

* every statement runs in its own transaction (``autocommit_block``), so no
  transaction ever holds an exclusive lock on two hot tables at once and no
  cycle can form at all;
* ``inference_grants`` is done first, matching the application's lock order,
  as defence in depth;
* both ``ADD COLUMN``s are metadata-only. PostgreSQL 11+ stores a non-volatile
  ``DEFAULT`` in ``pg_attribute.attmissingval`` instead of rewriting the heap,
  so ``NOT NULL DEFAULT 1`` on ``inference_grants`` never rewrites the table.
  Acquiring the lock was the only slow part there;
* the backfill walks ``ctid`` page ranges in small committed batches, taking
  only a ``RowExclusiveLock``, which coexists with the row locks the inference
  path holds;
* ``SET NOT NULL`` and ``SET DEFAULT 0`` land together in one short transaction
  behind an explicit ``LOCK TABLE``, bounded by ``lock_timeout`` and retried
  with backoff, so lock *acquisition* degrades into a retry instead of a
  deadlock;
* every step is idempotent (``IF NOT EXISTS``, ``WHERE ... IS NULL``, a
  ``SET NOT NULL`` that is a no-op when already set), so a run that dies
  between committed batches is safe to re-run from the top.

``DEFAULT 0`` is deliberately withheld until that final transaction. Set
earlier, rows inserted by the still-running old build during the backfill would
carry a zero ceiling, and ``finish_inference_request`` marks anything above its
ceiling non-deliverable -- a zero would 409 successful calls back to the
harness, which is the exact incident this deploy carries the fix for. Leaving
the column nullable until the end is inert instead: the old build never reads
it.

Revision ID: b7e41c93a05d
Revises: f3b8c2d17a49
Create Date: 2026-07-26
"""

import contextlib
import random
import time
from collections.abc import Sequence

from sqlalchemy import Connection, exc

from alembic import op

revision: str = "b7e41c93a05d"
down_revision: str | Sequence[str] | None = "f3b8c2d17a49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LOCK_TIMEOUT = "3s"
"""How long any one statement waits for a table lock before giving up.

Short on purpose. A statement that has been queued for an exclusive lock also
blocks every request queuing behind it, so a long wait is itself an outage. The
retry below is what makes a short timeout safe.
"""

MAX_ATTEMPTS = 12
"""Attempts per lock-taking statement, ~0.5s to ~8s of backoff between them."""

BACKFILL_PAGES = 2000
"""Heap pages per backfill batch, roughly 16MB of table at the default 8kB
page size. Small enough that one batch is a short write transaction, large
enough that a 262MB table is a couple of dozen batches rather than thousands."""

# SQLSTATEs worth retrying. 55P03 is `lock_not_available` -- our own
# `lock_timeout` firing, which is the designed outcome under contention. 40P01
# is `deadlock_detected`; the new shape should not be able to produce one, but
# retrying it is strictly better than failing a deploy if some other writer
# introduces a cycle we did not model.
RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01"})


def _sqlstate(error: BaseException) -> str | None:
    """Pull the SQLSTATE off a driver error, whichever driver raised it."""
    original = getattr(error, "orig", error)
    code = getattr(original, "sqlstate", None)
    if code is None:
        code = getattr(original, "pgcode", None)
    return str(code) if code is not None else None


def _retryable(error: BaseException) -> bool:
    return _sqlstate(error) in RETRYABLE_SQLSTATES


def _backoff(attempt: int) -> float:
    """Exponential backoff, capped, with jitter.

    Jitter matters here: three validators at three to four slots each produce a
    bursty load, and an unjittered retry lands on the same crest every time.
    """
    return min(0.5 * (2 ** (attempt - 1)), 8.0) * (0.5 + random.random())


def _with_retry(bind: Connection, statements: Sequence[str], what: str) -> None:
    """Run *statements* as one transaction, retrying on lock contention.

    Called from inside an ``autocommit_block``, so the transaction is opened
    with explicit SQL rather than by SQLAlchemy. On a retryable failure the
    whole transaction is rolled back -- releasing every lock it held, which is
    what lets the competing traffic drain -- and replayed after a backoff.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            bind.exec_driver_sql("BEGIN")
            bind.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            for statement in statements:
                bind.exec_driver_sql(statement)
            bind.exec_driver_sql("COMMIT")
            return
        except exc.DBAPIError as error:
            # Best-effort: a ROLLBACK that itself fails must not replace the
            # error that actually explains the failure. A deploy log that names
            # the wrong cause is worse than one that names none.
            with contextlib.suppress(exc.DBAPIError):
                bind.exec_driver_sql("ROLLBACK")
            if not _retryable(error) or attempt == MAX_ATTEMPTS:
                raise
            delay = _backoff(attempt)
            print(
                f"    {what}: {_sqlstate(error)} on attempt "
                f"{attempt}/{MAX_ATTEMPTS}; retrying in {delay:.1f}s"
            )
            time.sleep(delay)


def _backfill(bind: Connection) -> None:
    """Copy ``reserved_tokens`` into ``max_chargeable_tokens``, batch by batch.

    Walks the heap in ``ctid`` page ranges rather than repeatedly re-scanning
    for ``IS NULL`` with a ``LIMIT``: the range form is bounded work per batch
    and linear overall, where the ``LIMIT`` form rescans the already-backfilled
    prefix on every pass and degrades quadratically.

    The page count is read once, up front. Rows appended after that -- the old
    build is still serving throughout -- are not covered by the walk, so a
    convergence sweep follows, and whatever is still outstanding when the walk
    and sweep are done is handled by the residue update inside the final
    transaction, under the lock that makes it exhaustive.
    """
    pages = bind.exec_driver_sql(
        "SELECT pg_relation_size('inference_requests') "
        "/ current_setting('block_size')::bigint"
    ).scalar()
    total = int(pages or 0) + 1

    for start in range(0, total, BACKFILL_PAGES):
        end = start + BACKFILL_PAGES
        _with_retry(
            bind,
            [
                "UPDATE inference_requests "
                "SET max_chargeable_tokens = reserved_tokens "
                "WHERE max_chargeable_tokens IS NULL "
                f"AND ctid >= '({start},0)'::tid AND ctid < '({end},0)'::tid"
            ],
            f"backfill pages {start}-{end}",
        )

    # Rows appended past the original end of the heap while the walk ran.
    # Bounded: each pass takes the oldest outstanding batch, and the insert
    # rate is far below the rate this drains at.
    for _ in range(MAX_ATTEMPTS):
        remaining = bind.exec_driver_sql(
            "SELECT count(*) FROM inference_requests "
            "WHERE max_chargeable_tokens IS NULL"
        ).scalar()
        if not remaining:
            return
        _with_retry(
            bind,
            [
                "UPDATE inference_requests "
                "SET max_chargeable_tokens = reserved_tokens "
                "WHERE ctid IN ("
                "  SELECT ctid FROM inference_requests "
                "  WHERE max_chargeable_tokens IS NULL LIMIT 20000"
                ")"
            ],
            "backfill sweep",
        )


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        # `inference_grants` first: the application locks grants before
        # requests, and taking them in the same order is what keeps a cycle
        # from forming even if these two ever share a transaction again.
        # Metadata-only -- the default is a constant, so no heap rewrite.
        _with_retry(
            bind,
            [
                "ALTER TABLE inference_grants "
                "ADD COLUMN IF NOT EXISTS usage_accounting_version "
                "INTEGER NOT NULL DEFAULT 1"
            ],
            "inference_grants.usage_accounting_version",
        )

        # Nullable and undefaulted, so this is metadata-only too, and so the
        # old build keeps writing NULL (inert) rather than 0 (a zero ceiling,
        # which would 409 live calls) while the backfill runs.
        _with_retry(
            bind,
            [
                "ALTER TABLE inference_requests "
                "ADD COLUMN IF NOT EXISTS max_chargeable_tokens BIGINT"
            ],
            "inference_requests.max_chargeable_tokens",
        )

        _backfill(bind)

        # One short exclusive window. The explicit LOCK TABLE is what makes the
        # residue update and SET NOT NULL atomic with respect to inserts: sweep
        # and constrain in separate transactions and a row landing in between
        # fails the constraint. `SET NOT NULL` still scans the table, but it
        # scans a table it already holds, and a seq scan of 262MB is a
        # sub-second stall rather than the multi-second rewrite that deadlocked.
        _with_retry(
            bind,
            [
                "LOCK TABLE inference_requests IN ACCESS EXCLUSIVE MODE",
                "UPDATE inference_requests "
                "SET max_chargeable_tokens = reserved_tokens "
                "WHERE max_chargeable_tokens IS NULL",
                "ALTER TABLE inference_requests "
                "ALTER COLUMN max_chargeable_tokens SET NOT NULL, "
                "ALTER COLUMN max_chargeable_tokens SET DEFAULT 0",
            ],
            "inference_requests.max_chargeable_tokens NOT NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        # Separate transactions for the same reason the upgrade uses them: one
        # transaction dropping a column from both hot tables would reintroduce
        # the two-table exclusive lock that deadlocked.
        _with_retry(
            bind,
            [
                "ALTER TABLE inference_grants "
                "DROP COLUMN IF EXISTS usage_accounting_version"
            ],
            "drop inference_grants.usage_accounting_version",
        )
        _with_retry(
            bind,
            [
                "ALTER TABLE inference_requests "
                "DROP COLUMN IF EXISTS max_chargeable_tokens"
            ],
            "drop inference_requests.max_chargeable_tokens",
        )

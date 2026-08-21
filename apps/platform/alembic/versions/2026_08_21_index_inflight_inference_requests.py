"""index in-flight inference requests so observability stops scanning the ledger

Revision ID: 6570642aba4a
Revises: b7e2c91a04d6
Create Date: 2026-08-21

``/admin/inference-runtime-metrics`` counts requests still ``started`` past the
provider-timeout recovery window. That predicate has no index: ``status`` is not
in any key, and ``started_at < now() - 180s`` is the whole ledger. On
2026-08-21, with 15 million rows behind it, the count walked
``inference_requests_kind_started_idx`` from table birth, fetched every heap
tuple to test ``status``, and did not finish inside Backroom's 30-second budget
-- twice -- while the public endpoints answered in under a second.

The rows that qualify are a few dozen at any moment: a request is ``started``
for the length of one provider call, and the grant rotation, revocation sweep and
admission path all cancel whatever is left behind (``revoke_ticket_inference``,
``begin_inference_request``). A partial index over exactly those rows makes the
count an index-only walk of a handful of entries whatever the ledger holds, and
keeps the metric's meaning unchanged: it still counts every stuck request, not
only the recent ones.

The admission path's own global and per-validator in-flight counts test the same
``status = 'started' AND request_kind = ? AND started_at >= ?`` shape and may use
the index too; that is a planner choice, not something this revision relies on.

Built ``CONCURRENTLY``: ``inference_requests`` is the hottest table on the
platform and a plain ``CREATE INDEX`` holds a ``SHARE`` lock against every
``INSERT`` and ``UPDATE`` for the whole build of a 5 GB heap, which is an
inference outage. ``CONCURRENTLY`` cannot run inside a transaction, so this uses
an ``autocommit_block``, and it is not atomic: a build interrupted by a lock
timeout, a deploy kill, or the env.py replay leaves an ``INVALID`` index behind
that ``IF NOT EXISTS`` would then happily keep. Every step therefore re-checks
the catalog first and drops an invalid leftover before building, so the
migration is re-runnable from any point, which is what env.py's bounded retry
assumes of it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "6570642aba4a"
down_revision: str | Sequence[str] | None = "b7e2c91a04d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEX_NAME = "inference_requests_inflight_idx"
_INDEX_STATE_SQL = """
SELECT i.indisvalid
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname = :name
"""


def _index_state(bind) -> bool | None:  # noqa: ANN001 -- alembic bind
    """``True`` valid, ``False`` invalid leftover, ``None`` absent."""
    row = bind.execute(text(_INDEX_STATE_SQL), {"name": INDEX_NAME}).first()
    return None if row is None else bool(row[0])


def _run_concurrently(bind, statement: str, what: str) -> None:  # noqa: ANN001
    """Run one ``CONCURRENTLY`` statement, retrying lock contention.

    Only the initial ``SHARE UPDATE EXCLUSIVE`` acquisition is subject to
    ``lock_timeout``; the build itself is never cut short. ``autovacuum`` is the
    usual holder of that lock on this table and yields to us on its own, so
    the retry here is the same bounded backoff the column helpers use, not a
    long wait.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            bind.exec_driver_sql(statement)
            return
        except exc.DBAPIError as error:
            if not is_retryable(error) or attempt == MAX_ATTEMPTS:
                raise
            delay = backoff_delay(attempt)
            log.warning(
                "%s: %s on attempt %d/%d; retrying in %.1fs",
                what,
                sqlstate(error),
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        state = _index_state(bind)
        if state is False:
            log.warning(
                "%s is INVALID from an interrupted build; rebuilding", INDEX_NAME
            )
            _run_concurrently(
                bind,
                f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}",
                f"drop invalid {INDEX_NAME}",
            )
            state = None
        if state is None:
            _run_concurrently(
                bind,
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                "ON inference_requests (request_kind, started_at) "
                "WHERE status = 'started'",
                f"create {INDEX_NAME}",
            )
        if _index_state(bind) is not True:
            raise RuntimeError(
                f"{INDEX_NAME} did not come up valid; re-run the migration"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _run_concurrently(
            op.get_bind(),
            f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}",
            f"drop {INDEX_NAME}",
        )

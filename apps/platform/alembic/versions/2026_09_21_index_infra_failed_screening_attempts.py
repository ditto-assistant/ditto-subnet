"""index infrastructure-failed screening attempts for the fleet breaker

Revision ID: 3c9d5e7a1b42
Revises: e804a171db92
Create Date: 2026-09-21

``claim_screening_attempts`` derives the docker-build-infrastructure fleet
breaker from attempt history while holding the global claim advisory lock, so its
``status = 'failed' AND reason_code = 'docker-build-infrastructure' AND
finished_at >= :cutoff`` scan must not walk the table: ``screening_attempts`` is
indexed only by ``(agent_id, started_at)`` and one-running-per-agent. The rows
that qualify are every historical infrastructure failure of that one code (still
a tiny fraction of the table), so a partial index over exactly them makes the scan
a range walk of a handful of entries.

The predicate is duplicated in ``models.py`` and
``screening_infra_retry._infra_failure_filters``; keep all three in step.

Built ``CONCURRENTLY`` (an ``autocommit_block``) so it never holds a ``SHARE``
lock against attempt inserts and verdict updates, and re-runnable from any
point: an interrupted build leaves an ``INVALID`` index that is dropped first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "3c9d5e7a1b42"
down_revision: str | Sequence[str] | None = "e804a171db92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEX_NAME = "screening_attempts_infra_failed_idx"
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
                "ON screening_attempts (finished_at) "
                "WHERE status = 'failed' "
                "AND reason_code = 'docker-build-infrastructure'",
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

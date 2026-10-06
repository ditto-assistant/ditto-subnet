"""Recover one known interrupted concurrent-build failure without editing history.

The published audit-index migration can leave INVALID state and then skip it
on CREATE IF NOT EXISTS. Repair only its exact validation failure, on the same
migration connection after rollback, then resume Alembic's recorded history.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sqlalchemy import Connection, exc, text

from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

log = logging.getLogger("alembic.lock")
INDEX_NAME = "score_audit_log_event_agent_idx"
FAILURE = f"{INDEX_NAME} did not come up valid; re-run the migration"


def _index_state(bind: Connection) -> bool | None:
    value = bind.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relname=:name"
        ),
        {"name": INDEX_NAME},
    ).first()
    return None if value is None else bool(value[0])


def _ensure_valid_index(bind: Connection) -> None:
    # Re-read after EVERY failed statement. CREATE can leave an invalid index,
    # and DROP can complete some of its phases before a later wait times out.
    # Neither result can be treated as the pre-attempt state on the next try.
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            state = _index_state(bind)
            if state is True:
                return
            if state is False:
                log.warning("%s is INVALID; rebuilding", INDEX_NAME)
                bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
            bind.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                "ON score_audit_log (event, agent_id)"
            )
            if _index_state(bind) is True:
                return
            # A second builder can publish an INVALID index between our read
            # and IF NOT EXISTS. Retry the catalog, not that stale DDL outcome.
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(FAILURE)
            log.warning("%s still not valid; rechecking catalog", INDEX_NAME)
            time.sleep(backoff_delay(attempt))
        except exc.DBAPIError as error:
            if not is_retryable(error) or attempt == MAX_ATTEMPTS:
                raise
            delay = backoff_delay(attempt)
            log.warning(
                "build %s: %s on attempt %d/%d; rechecking catalog in %.1fs",
                INDEX_NAME,
                sqlstate(error),
                attempt,
                MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)


def run_with_audit_index_recovery(
    connection: Connection, run: Callable[[Connection], None]
) -> None:
    try:
        run(connection)
        return
    except RuntimeError as error:
        if str(error) != FAILURE:
            raise
        connection.rollback()
        if _index_state(connection) is True:
            connection.rollback()
            raise
        connection.rollback()
        isolation = connection.get_isolation_level()
        connection.rollback()
        try:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            _ensure_valid_index(connection)
        finally:
            connection.rollback()
            connection.execution_options(isolation_level=isolation)
    # Prior revisions committed by Alembic remain recorded; no schema stamp or
    # skip. The unchanged historical migration observes a valid index and runs.
    run(connection)

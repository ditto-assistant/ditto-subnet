"""index score audit log by event and agent for admission probes

Revision ID: b4c9f7e2a810
Revises: e2b5f0a9467c
Create Date: 2026-10-06

``benchmark_admission_predicate``'s ``contract_refresh`` arm probes the
append-only ``score_audit_log`` for one exact ``event`` string per ``agent_id``
during every screening claim and validator allocation. The existing
``score_audit_log_agent_id_idx`` on ``(agent_id)`` alone makes that probe an
index scan plus a recheck of every historical event the agent ever recorded;
the two-column index turns it into a point lookup. The production audit
measured the empty screening claim at 6.5-13.7 s inside the claim transaction,
with this probe repeated per candidate agent.

Built ``CONCURRENTLY`` (an ``autocommit_block``) so it never holds a ``SHARE``
lock against the audit log's append path, and re-runnable from any point: an
interrupted build leaves an ``INVALID`` index that is dropped first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "b4c9f7e2a810"
down_revision: str | Sequence[str] | None = "e2b5f0a9467c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEX_NAME = "score_audit_log_event_agent_idx"
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

    A concurrent build can also time out waiting for older writers/snapshots
    after committing an INVALID catalog entry. Creation therefore uses the
    state-aware loop below, not a blind retry of ``IF NOT EXISTS``.
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


def _ensure_valid_index(bind) -> None:  # noqa: ANN001 -- alembic bind
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
            if _index_state(bind) is not True:
                raise RuntimeError(
                    f"{INDEX_NAME} did not come up valid; re-run the migration"
                )
            return
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


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _ensure_valid_index(op.get_bind())


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _run_concurrently(
            op.get_bind(),
            f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}",
            f"drop {INDEX_NAME}",
        )

"""cover worker-claim-not-started in the infra-failed attempts index

Revision ID: e0f28816bca9
Revises: 4c917e2a6b0d
Create Date: 2026-09-29

``worker-claim-not-started`` joins ``INFRA_AUTO_RETRY_REASON_CODES`` (#2446), so
the fleet breaker's ``status = 'failed' AND reason_code IN (...)`` scan under
the global claim lock must still be implied by the partial index predicate.
A partial index predicate cannot be altered, so the widened index is built
``CONCURRENTLY`` under a temporary name, the old one is dropped
``CONCURRENTLY``, and the new one takes its name. The scan keeps an index
throughout and nothing holds a ``SHARE`` lock against attempt writes.

The predicate is duplicated in ``models.py`` and
``screening_infra_retry._infra_failure_filters``; keep all three in step.

Re-runnable from any point: an interrupted build leaves an ``INVALID``
temporary index that is dropped first, and a completed swap is simply redone.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "e0f28816bca9"
down_revision: str | Sequence[str] | None = "4c917e2a6b0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEX_NAME = "screening_attempts_infra_failed_idx"
_BUILD_NAME = "screening_attempts_infra_failed_swap_idx"
_WIDE_PREDICATE = (
    "status = 'failed' AND reason_code IN "
    "('docker-build-infrastructure', 'worker-claim-not-started')"
)
_NARROW_PREDICATE = "status = 'failed' AND reason_code = 'docker-build-infrastructure'"
_INDEX_STATE_SQL = """
SELECT i.indisvalid
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname = :name
"""


def _index_state(bind, name: str) -> bool | None:  # noqa: ANN001 -- alembic bind
    """``True`` valid, ``False`` invalid leftover, ``None`` absent."""
    row = bind.execute(text(_INDEX_STATE_SQL), {"name": name}).first()
    return None if row is None else bool(row[0])


def _run_concurrently(bind, statement: str, what: str) -> None:  # noqa: ANN001
    """Run one statement, retrying lock contention with the shared backoff."""
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


def _swap(predicate: str) -> None:
    """Replace ``INDEX_NAME`` with an index on ``predicate``, never going bare."""
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        if _index_state(bind, _BUILD_NAME) is False:
            log.warning(
                "%s is INVALID from an interrupted build; rebuilding", _BUILD_NAME
            )
            _run_concurrently(
                bind,
                f"DROP INDEX CONCURRENTLY IF EXISTS {_BUILD_NAME}",
                f"drop invalid {_BUILD_NAME}",
            )
        _run_concurrently(
            bind,
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_BUILD_NAME} "
            f"ON screening_attempts (finished_at) WHERE {predicate}",
            f"create {_BUILD_NAME}",
        )
        if _index_state(bind, _BUILD_NAME) is not True:
            raise RuntimeError(
                f"{_BUILD_NAME} did not come up valid; re-run the migration"
            )
        _run_concurrently(
            bind,
            f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}",
            f"drop {INDEX_NAME}",
        )
        _run_concurrently(
            bind,
            f"ALTER INDEX {_BUILD_NAME} RENAME TO {INDEX_NAME}",
            f"rename {_BUILD_NAME}",
        )


def upgrade() -> None:
    _swap(_WIDE_PREDICATE)


def downgrade() -> None:
    _swap(_NARROW_PREDICATE)

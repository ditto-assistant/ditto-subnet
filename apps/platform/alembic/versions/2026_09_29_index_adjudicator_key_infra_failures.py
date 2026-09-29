"""cover node court-key failures in the infra-failure index

Revision ID: 6a7a2a03a65f
Revises: 5e2a8c4f9d17
Create Date: 2026-09-29

``INFRA_AUTO_RETRY_REASON_CODES`` now also retries a worker's
``source-review-adjudicator-key-unavailable`` failure: the source-review court's
API key file on that node was unset, unreadable, too short, or readable by
group/other, so the court never started (#2449). The fleet breaker scan under
the global claim lock filters on exactly that tuple, so the partial
``screening_attempts_infra_failed_idx`` from ``5e2a8c4f9d17`` must name all four
codes or the scan falls back to a sequential walk.

The predicate is duplicated in ``models.py`` and
``screening_infra_retry._infra_failure_filters``; keep all three in step.

Build the replacement ``CONCURRENTLY`` under a temporary name before dropping
the old index, so the claim-lock breaker scan retains an index throughout.
Re-runnable from an invalid temporary build or an interrupted rename.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "6a7a2a03a65f"
down_revision: str | Sequence[str] | None = "5e2a8c4f9d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEX_NAME = "screening_attempts_infra_failed_idx"
BUILD_NAME = "screening_attempts_infra_failed_swap_idx"
PREVIOUS_CODES = (
    "docker-build-infrastructure",
    "worker-claim-not-started",
    "l2-runtime-evidence-unavailable",
)
NEW_CODE = "source-review-adjudicator-key-unavailable"


def _predicate(codes: Sequence[str]) -> str:
    return (
        "status = 'failed' AND reason_code IN ("
        + ", ".join(f"'{code}'" for code in codes)
        + ")"
    )


UPGRADED_PREDICATE = _predicate((*PREVIOUS_CODES, NEW_CODE))
PREVIOUS_PREDICATE = _predicate(PREVIOUS_CODES)
_INDEX_STATE_SQL = """
SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid)
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname = :name
"""


def _index_state(bind, name: str) -> tuple[bool, str] | None:  # noqa: ANN001
    """``(valid, predicate)`` for the index, or ``None`` when it is absent."""
    row = bind.execute(text(_INDEX_STATE_SQL), {"name": name}).first()
    return None if row is None else (bool(row[0]), str(row[1] or ""))


def _run_concurrently(bind, statement: str, what: str) -> None:  # noqa: ANN001
    """Run one ``CONCURRENTLY`` statement, retrying lock contention."""
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


def _rebuild(predicate: str, *, covers_new_code: bool) -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()

        def matches(name: str) -> bool:
            state = _index_state(bind, name)
            return (
                state is not None
                and state[0]
                and all(f"'{code}'" in state[1] for code in PREVIOUS_CODES)
                and (f"'{NEW_CODE}'" in state[1]) is covers_new_code
            )

        if matches(INDEX_NAME):
            if _index_state(bind, BUILD_NAME) is not None:
                _run_concurrently(
                    bind,
                    f"DROP INDEX CONCURRENTLY IF EXISTS {BUILD_NAME}",
                    f"drop leftover {BUILD_NAME}",
                )
            return

        if not matches(BUILD_NAME):
            if _index_state(bind, BUILD_NAME) is not None:
                log.warning("%s is invalid or stale; rebuilding", BUILD_NAME)
                _run_concurrently(
                    bind,
                    f"DROP INDEX CONCURRENTLY IF EXISTS {BUILD_NAME}",
                    f"drop {BUILD_NAME}",
                )
            _run_concurrently(
                bind,
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {BUILD_NAME} "
                f"ON screening_attempts (finished_at) WHERE {predicate}",
                f"create {BUILD_NAME}",
            )
        if not matches(BUILD_NAME):
            raise RuntimeError(f"{BUILD_NAME} did not come up valid")
        _run_concurrently(
            bind,
            f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}",
            f"drop {INDEX_NAME}",
        )
        _run_concurrently(
            bind,
            f"ALTER INDEX {BUILD_NAME} RENAME TO {INDEX_NAME}",
            f"rename {BUILD_NAME}",
        )
        if not matches(INDEX_NAME):
            raise RuntimeError(f"{INDEX_NAME} did not come up valid")


def upgrade() -> None:
    _rebuild(UPGRADED_PREDICATE, covers_new_code=True)


def downgrade() -> None:
    _rebuild(PREVIOUS_PREDICATE, covers_new_code=False)

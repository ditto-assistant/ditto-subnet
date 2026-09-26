"""Index the per-ticket and per-validator inference RPM rails.

Why
---
The relay's admission path counts recent ``inference_requests`` twice per
request: once per grant (``CountRecentTicketRequests``) and once per validator
(``CountRecentValidatorRequests``). Both are unlocked, best-effort reads, yet the
2026-09-07 Postgres slow log recorded 5,200 executions of the validator count
above 500 ms (avg 623 ms, max 3.1 s). ``inference_requests`` holds tens of
millions of settled rows (28.5M at last ANALYZE, 5.8 GB heap, 4.5 GB of
indexes) and the only usable paths were the ``(grant_id, nonce)`` primary key
and ``started_at`` alone, so each count walked far more pages than the few
dozen rows it returned.

What
----
``inference_requests_grant_kind_started_idx (grant_id, request_kind,
started_at)`` answers the per-grant rail as an index range scan and lets the
per-validator rail run grant-first. ``inference_grants_validator_hotkey_idx
(validator_hotkey)`` is what "grant-first" needs: today the only index carrying
``validator_hotkey`` has it as the third column of the lease key, so a lookup by
validator alone scans all 49k grants.

Both are built ``CONCURRENTLY`` for the same reason the 2026_08_21 in-flight
index was: ``inference_requests`` is the hottest table on the relay path and a
blocking build on it is an inference outage. ``CONCURRENTLY`` cannot run inside
a transaction, so this uses an ``autocommit_block`` and is not atomic; an
interrupted build leaves an INVALID index, which the upgrade detects, drops, and
rebuilds, so re-running the migration converges. Retention for the settled-row
ledger itself is a separate Platform job, not this revision.

Revision ID: 7c2d9e4f1a58
Revises: b3f926ce145a
Create Date: 2026-09-08
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "7c2d9e4f1a58"
down_revision: str | Sequence[str] | None = "b3f926ce145a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

INDEXES: tuple[tuple[str, str], ...] = (
    (
        "inference_requests_grant_kind_started_idx",
        "ON inference_requests (grant_id, request_kind, started_at)",
    ),
    (
        "inference_grants_validator_hotkey_idx",
        "ON inference_grants (validator_hotkey)",
    ),
)

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
    """Run one ``CONCURRENTLY`` statement, retrying lock contention.

    Only the initial ``SHARE UPDATE EXCLUSIVE`` acquisition is subject to
    ``lock_timeout``; the build itself is never cut short.
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
        for name, definition in INDEXES:
            state = _index_state(bind, name)
            if state is False:
                log.warning("%s is INVALID from an interrupted build; rebuilding", name)
                _run_concurrently(
                    bind,
                    f"DROP INDEX CONCURRENTLY IF EXISTS {name}",
                    f"drop invalid {name}",
                )
                state = None
            if state is None:
                _run_concurrently(
                    bind,
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}",
                    f"create {name}",
                )
            if _index_state(bind, name) is not True:
                raise RuntimeError(f"{name} did not become a valid index")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, _definition in reversed(INDEXES):
            _run_concurrently(
                bind,
                f"DROP INDEX CONCURRENTLY IF EXISTS {name}",
                f"drop {name}",
            )

"""index agent name and payment coldkey for screening-submission search

Revision ID: 7bc0400d90c2
Revises: c3a91e7b2d40
Create Date: 2026-09-26

``GET /admin/screening-submissions`` filters by exact ``agent_name``, literal
``agent_name_prefix``, and payment-time ``miner_coldkey`` (#560). ``agents`` had
no index led by ``name`` (the ``(miner_hotkey, name, version)`` unique key is
hotkey-first), and ``evaluation_payments`` had none on ``miner_coldkey``.

``agents_name_pattern_idx`` uses ``text_pattern_ops``: the database collation is
not ``C``, so a default-opclass btree cannot serve ``name LIKE 'prefix%'``;
the pattern opclass serves both that prefix range and exact equality.
``agents_sha256_idx`` and ``agents_miner_hotkey_idx`` already back the other
exact filters.

Both are built ``CONCURRENTLY`` (an ``autocommit_block``) so neither holds a
``SHARE`` lock against uploads or payment writes, and the migration is
re-runnable from any point: an interrupted build leaves an ``INVALID`` index
that is dropped and rebuilt.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from sqlalchemy import exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "7bc0400d90c2"
down_revision: str | Sequence[str] | None = "c3a91e7b2d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

# (index name, CREATE target) in build order. Keep in step with models.py.
INDEXES: tuple[tuple[str, str], ...] = (
    ("agents_name_pattern_idx", "ON agents (name text_pattern_ops)"),
    (
        "evaluation_payments_miner_coldkey_idx",
        "ON evaluation_payments (miner_coldkey)",
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


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, target in INDEXES:
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
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {target}",
                    f"create {name}",
                )
            if _index_state(bind, name) is not True:
                raise RuntimeError(
                    f"{name} did not come up valid; re-run the migration"
                )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, _target in reversed(INDEXES):
            _run_concurrently(
                bind,
                f"DROP INDEX CONCURRENTLY IF EXISTS {name}",
                f"drop {name}",
            )

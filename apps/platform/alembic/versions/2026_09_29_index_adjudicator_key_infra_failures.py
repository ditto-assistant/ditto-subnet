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

A concurrent build is not atomic. A lock timeout or deadlock in any of its waits
cancels it after its catalog entry committed, leaving an INVALID index under the
build name, and a retried ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` then
"succeeds" on that leftover without building anything. So every attempt re-reads
the catalog and drops whatever sits under the build name unless it is valid with
exactly the target definition (table, access method, key column, uniqueness, and
the predicate's code set), then builds again. The same check lets a re-run
recover from an interrupted build, drop, or rename, and replaces a stale index
under either name rather than keeping or swapping it in.

The ``CONCURRENTLY`` statements run under a longer, still bounded
``lock_timeout`` than env.py's session default. That short default protects
traffic from a statement queued for an exclusive table lock. A concurrent build
or drop takes only ``SHARE UPDATE EXCLUSIVE`` on the table, which conflicts with
DDL, VACUUM, and ANALYZE but never with reads or writes, and its waits for older
transactions block nobody. Under the short default, any transaction in the
database that outlives it cancels the build.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import NamedTuple

from sqlalchemy import Connection, exc, text

from alembic import op
from ditto.db.migration_lock import MAX_ATTEMPTS, backoff_delay, is_retryable, sqlstate

revision: str = "6a7a2a03a65f"
down_revision: str | Sequence[str] | None = "5e2a8c4f9d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.lock")

TABLE = "screening_attempts"
COLUMN = "finished_at"
INDEX_NAME = "screening_attempts_infra_failed_idx"
BUILD_NAME = "screening_attempts_infra_failed_swap_idx"
PREVIOUS_CODES = (
    "docker-build-infrastructure",
    "worker-claim-not-started",
    "l2-runtime-evidence-unavailable",
)
NEW_CODE = "source-review-adjudicator-key-unavailable"
UPGRADED_CODES = (*PREVIOUS_CODES, NEW_CODE)

# Bounds each wait of a CONCURRENTLY statement: long enough to outlast ordinary
# request transactions (Backroom's slowest reads have a 30s budget), short
# enough that a session left idle in a transaction fails the attempt instead of
# hanging the deploy.
_CONCURRENT_LOCK_TIMEOUT = "30s"

_INDEX_STATE_SQL = """
SELECT i.indisvalid,
       t.relname = :table
         AND am.amname = 'btree'
         AND NOT i.indisunique
         AND i.indexprs IS NULL
         AND i.indnatts = 1
         AND pg_get_indexdef(i.indexrelid, 1, true) = :column,
       pg_get_expr(i.indpred, i.indrelid)
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_am am ON am.oid = c.relam
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = current_schema()
   AND c.relname = :name
"""

# PostgreSQL's rendering of ``_predicate`` for two or more codes.
_RENDERED_PREDICATE = re.compile(
    r"\(\(status = 'failed'::text\) AND "
    r"\(reason_code = ANY \(ARRAY\[(?P<codes>[^\]]*)\]\)\)\)"
)
_RENDERED_CODE = re.compile(r"'(?P<code>[a-z0-9-]+)'::text")


def _predicate(codes: Sequence[str]) -> str:
    return (
        "status = 'failed' AND reason_code IN ("
        + ", ".join(f"'{code}'" for code in codes)
        + ")"
    )


def _rendered_codes(predicate: str) -> list[str] | None:
    """The codes in a rendered ``_predicate``, or ``None`` for any other shape."""
    match = _RENDERED_PREDICATE.fullmatch(predicate)
    if match is None:
        return None
    codes = []
    for item in match.group("codes").split(", "):
        code = _RENDERED_CODE.fullmatch(item)
        if code is None:
            return None
        codes.append(code.group("code"))
    return codes


class _IndexState(NamedTuple):
    valid: bool
    # One plain, non-unique btree key column, ``COLUMN``, on ``TABLE``.
    shape_matches: bool
    predicate: str

    def matches(self, codes: Sequence[str]) -> bool:
        rendered = _rendered_codes(self.predicate)
        return (
            self.valid
            and self.shape_matches
            and rendered is not None
            and sorted(rendered) == sorted(codes)
        )


def _index_state(bind: Connection, name: str) -> _IndexState | None:
    """The index's validity and definition, or ``None`` when it is absent."""
    row = bind.execute(
        text(_INDEX_STATE_SQL), {"name": name, "table": TABLE, "column": COLUMN}
    ).first()
    if row is None:
        return None
    return _IndexState(
        valid=bool(row[0]), shape_matches=bool(row[1]), predicate=str(row[2] or "")
    )


@contextlib.contextmanager
def _concurrent_lock_timeout(bind: Connection) -> Iterator[None]:
    """Widen ``lock_timeout`` for one CONCURRENTLY statement, then restore it.

    This runs in an autocommit block, so ``SET LOCAL`` has no transaction to
    scope to. ``RESET`` returns to the session default, which is env.py's
    startup parameter, so the migrations after this one keep the short bound.
    """
    bind.exec_driver_sql(f"SET lock_timeout = '{_CONCURRENT_LOCK_TIMEOUT}'")
    try:
        yield
    finally:
        bind.exec_driver_sql("RESET lock_timeout")


def _with_retry(what: str, step: Callable[[], object]) -> None:
    """Run ``step``, retrying lock contention with the shared backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            step()
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


def _drop(bind: Connection, name: str) -> None:
    """Drop ``name`` concurrently if present.

    A cancelled drop leaves the index INVALID, which the retry drops again.
    """

    def attempt() -> None:
        with _concurrent_lock_timeout(bind):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")

    _with_retry(f"drop {name}", attempt)


def _build(bind: Connection, name: str, codes: Sequence[str]) -> None:
    """Build ``name`` concurrently until it is valid with exactly ``codes``."""

    def attempt() -> None:
        state = _index_state(bind, name)
        if state is not None and state.matches(codes):
            return
        with _concurrent_lock_timeout(bind):
            if state is not None:
                # Invalid from a cancelled build, or stale: never keep it.
                log.warning("%s is invalid or stale; rebuilding it", name)
                bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            bind.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY {name} "
                f"ON {TABLE} ({COLUMN}) WHERE {_predicate(codes)}"
            )

    _with_retry(f"build {name}", attempt)
    state = _index_state(bind, name)
    if state is None or not state.matches(codes):
        raise RuntimeError(f"{name} did not come up valid as built: {state}")


def _rebuild(codes: Sequence[str]) -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        target = _index_state(bind, INDEX_NAME)
        if target is not None and target.matches(codes):
            # Already swapped (a re-run, or a replay after the rename).
            if _index_state(bind, BUILD_NAME) is not None:
                _drop(bind, BUILD_NAME)
            return
        _build(bind, BUILD_NAME, codes)
        _drop(bind, INDEX_NAME)
        _with_retry(
            f"rename {BUILD_NAME}",
            lambda: bind.exec_driver_sql(
                f"ALTER INDEX {BUILD_NAME} RENAME TO {INDEX_NAME}"
            ),
        )
        target = _index_state(bind, INDEX_NAME)
        if target is None or not target.matches(codes):
            raise RuntimeError(f"{INDEX_NAME} did not come up valid: {target}")


def upgrade() -> None:
    _rebuild(UPGRADED_CODES)


def downgrade() -> None:
    _rebuild(PREVIOUS_CODES)

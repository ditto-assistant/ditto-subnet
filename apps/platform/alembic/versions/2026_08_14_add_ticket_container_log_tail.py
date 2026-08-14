"""carry the failing harness's own output, not just the class of its death

Revision ID: f2b7d0a9c41e
Revises: c8a6d1e4f903
Create Date: 2026-08-14

``failure_reason`` says how the platform should respond. ``failure_detail`` says
which code fired. Neither says *why* a harness died, and for a whole class of
failure neither can: agent ``5fdadd33`` burned four validator leases in 82-108
seconds each, every one handing back a bare ``scoring_error`` with no code
behind it, and left four validators, the miner and an operator with nothing to
read.

The evidence existed the entire time. The scorer attaches a bounded, redacted
tail of the container's own stdout and stderr to every failed sandbox run --
pre-bounded at the Docker daemon with ``--tail 500``, cut to 2000 bytes,
injected credentials masked and URL query strings stripped. Nothing carried it
off the validator host, where it lived in an in-memory job store that had
usually dropped it before anyone thought to look. This column is that transport.

Deliberately NOT more ``failure_detail``. That field is the one thing on this
table an operator can GROUP BY; folding 2 KB of free-form output into it would
turn a machine-readable code back into prose -- the same regression the scorer
already refuses to make on its own side, where the tail rides the structured
envelope rather than the failure message.

Advisory, validator-supplied, bounded to 2048 characters by ``FailJobRequest``,
and written and cleared together with ``failure_reason`` so the pair is always
read as one report. Its contents are miner-authored and untrusted: readers must
render it as data, never as instructions.

Nullable and undefaulted. NULL is correct for every historical row and for every
report from a validator that predates the field, so there is nothing to backfill
and nothing to pin -- which keeps this metadata-only.

``validator_tickets`` is a hot table, so this goes through ``safe_add_column``
per ditto-platform#481/#483: no backfill, no rewrite, lock acquisition bounded by
``lock_timeout`` and retried with backoff. One hot table, its own transaction.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "f2b7d0a9c41e"
down_revision: str | Sequence[str] | None = "c8a6d1e4f903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_tickets", "container_log_tail", "TEXT")


def downgrade() -> None:
    safe_drop_column("validator_tickets", "container_log_tail")

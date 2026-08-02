"""keep the reporter's own failure code instead of throwing it away

Revision ID: a7c14f8bd260
Revises: b2e9d4a17c60
Create Date: 2026-07-27

``failure_reason`` is a three-value class picked to drive reissue policy, so it
answers "how should the platform respond" and nothing at all about "what
happened". ditto-subnet#279 classified twelve dead ``mnemo*`` leases as
``fail_job(reason="infrastructure")`` and still could not name the fault: the
validator knows which of its five sandbox infrastructure codes fired, the wire
had nowhere to put it, and the code survived only in a log line on the validator
host. The ~60-minute killer is unidentified for exactly that reason.

``failure_detail`` is that code. Advisory, validator-supplied, bounded to 200
characters by ``FailJobRequest``, and written and cleared together with
``failure_reason`` so the pair is always read as one report.

Nullable and undefaulted. NULL is the correct value for every historical row and
for every report from a validator that predates the field, so there is nothing
to backfill and nothing to pin -- which also keeps this metadata-only.

``validator_tickets`` is a hot table, so this goes through ``safe_add_column``
per ditto-platform#481/#483: no backfill, no rewrite, lock acquisition bounded by
``lock_timeout`` and retried with backoff. One hot table, its own transaction.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "a7c14f8bd260"
down_revision: str | Sequence[str] | None = "b2e9d4a17c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_tickets", "failure_detail", "TEXT")


def downgrade() -> None:
    safe_drop_column("validator_tickets", "failure_detail")

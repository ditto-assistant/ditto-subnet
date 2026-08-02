"""record when a lease was first observed running, so silence is not idleness

Revision ID: f3b6a80c95d1
Revises: c8a2f640d31e
Create Date: 2026-07-26

The lease liveness gate can only revoke on positive evidence of idleness, but it
had no way to tell "this slot has been running since before the last heartbeat"
apart from "this slot has never once been seen running". Both look identical in
a single capacity blob: an absent slot id.

They are not the same claim. A lease that has never reported has produced no
evidence in either direction -- it may still be pulling the screened image,
rendering its dataset, or seeding (the v7 seed alone is allowed 15 minutes,
three times the reporting grace). Revoking it destroys a healthy run. A lease
that reported and then went quiet has at least testified once, so its later
absence is a real signal.

``first_reported_at`` records the first moment heartbeat ingest confirmed this
lease against a live slot, which is exactly the line between those two cases.

Nullable and undefaulted on purpose. Every lease live at deploy time reads as
never-reported and therefore becomes unrevocable until it next reports, which is
the safe direction: the 90-minute deadline and ``expire_overdue_tickets`` still
reclaim a genuinely dead validator's slot, so the cost of a wrong NULL is a slot
held to its deadline, not a destroyed run.

``validator_tickets`` is a hot table, so this goes through ``safe_add_column``
per ditto-platform#481/#483: metadata-only, no backfill, no rewrite, lock
acquisition bounded by ``lock_timeout`` and retried with backoff. It touches
exactly one hot table in its own transaction.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "f3b6a80c95d1"
down_revision: str | Sequence[str] | None = "c8a2f640d31e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_tickets", "first_reported_at", "TIMESTAMPTZ")


def downgrade() -> None:
    safe_drop_column("validator_tickets", "first_reported_at")

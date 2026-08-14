"""persist signed validator confirmation progress

Revision ID: a4d922e7b116
Revises: c8a6d1e4f903
Create Date: 2026-08-14

The nullable JSONB column is metadata-only: existing heartbeats remain valid and
read as "progress not reported" until their validators negotiate protocol v22.
No row is rewritten or backfilled.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "a4d922e7b116"
down_revision: str | Sequence[str] | None = "c8a6d1e4f903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_heartbeats", "confirmation_progress", "JSONB")


def downgrade() -> None:
    safe_drop_column("validator_heartbeats", "confirmation_progress")

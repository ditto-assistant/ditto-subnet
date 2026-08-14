"""add signed sanitized validator updater status

Revision ID: d94f2c7a1e08
Revises: a4d922e7b116
Create Date: 2026-08-14

The nullable JSONB column preserves every older validator as NULL. It stores a
closed heartbeat model only; paths, logs, environment values and arbitrary
errors never enter the API contract. ``validator_heartbeats`` is hot, so the
metadata-only add/drop uses the bounded-lock migration helpers.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "d94f2c7a1e08"
down_revision: str | Sequence[str] | None = "a4d922e7b116"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_heartbeats", "updater_status", "JSONB")


def downgrade() -> None:
    safe_drop_column("validator_heartbeats", "updater_status")

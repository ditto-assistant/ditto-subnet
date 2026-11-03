"""Persist the original selector for validator retry idempotency.

Revision ID: 8d91f2b4c6a0
Revises: 6a35116ca9ae
Create Date: 2026-10-21

Existing recoveries were untargeted and remain NULL. New targeted grants store
exactly the requested selector independently of the selected ticket list.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "8d91f2b4c6a0"
down_revision: str | Sequence[str] | None = "6a35116ca9ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_retry_recoveries", "target_validator_hotkey", "TEXT")


def downgrade() -> None:
    safe_drop_column("validator_retry_recoveries", "target_validator_hotkey")

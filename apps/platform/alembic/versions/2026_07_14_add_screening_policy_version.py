"""version anti-cheat screening verdicts

Revision ID: a9e7c4d21b80
Revises: f4a8c2d91e60
Create Date: 2026-07-14 15:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a9e7c4d21b80"
down_revision: str | Sequence[str] | None = "f4a8c2d91e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Mark existing submissions for automatic policy-v2 re-screening."""
    op.execute(
        "ALTER TABLE agents ADD COLUMN screening_policy_version "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS screening_policy_version")

"""add miner-private screening failure feedback

Revision ID: e7a1c94b2d60
Revises: c4f1a92e7b63
Create Date: 2026-08-28 22:45:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7a1c94b2d60"
down_revision: str | Sequence[str] | None = "c4f1a92e7b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE screening_attempts ADD COLUMN failure_provider TEXT")
    op.execute("ALTER TABLE screening_attempts ADD COLUMN failure_lane TEXT")
    op.execute("ALTER TABLE screening_attempts ADD COLUMN private_failure_detail TEXT")
    op.execute(
        "ALTER TABLE screening_attempts ADD COLUMN private_failure_log_tail TEXT"
    )
    op.execute(
        "ALTER TABLE screening_attempts ADD COLUMN failure_captured_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE screening_attempts DROP COLUMN IF EXISTS failure_captured_at"
    )
    op.execute(
        "ALTER TABLE screening_attempts DROP COLUMN IF EXISTS private_failure_log_tail"
    )
    op.execute(
        "ALTER TABLE screening_attempts DROP COLUMN IF EXISTS private_failure_detail"
    )
    op.execute("ALTER TABLE screening_attempts DROP COLUMN IF EXISTS failure_lane")
    op.execute("ALTER TABLE screening_attempts DROP COLUMN IF EXISTS failure_provider")

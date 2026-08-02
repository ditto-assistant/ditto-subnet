"""add validator ticket retry budget

Revision ID: d8f1b6c24a70
Revises: c7e4a91d2b60
Create Date: 2026-07-14 10:15:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f1b6c24a70"
down_revision: str | Sequence[str] | None = "c7e4a91d2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE validator_tickets "
        "ADD COLUMN bench_version INTEGER NOT NULL DEFAULT 2, "
        "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1, "
        "ADD COLUMN retry_after TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE validator_tickets ADD CONSTRAINT "
        "validator_tickets_bench_version_positive CHECK (bench_version > 0)"
    )
    op.execute(
        "ALTER TABLE validator_tickets ADD CONSTRAINT "
        "validator_tickets_attempt_count_positive CHECK (attempt_count > 0)"
    )
    op.execute(
        "UPDATE validator_tickets "
        "SET retry_after = deadline + INTERVAL '6 hours' "
        "WHERE status = 'expired'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE validator_tickets "
        "DROP CONSTRAINT validator_tickets_attempt_count_positive, "
        "DROP CONSTRAINT validator_tickets_bench_version_positive, "
        "DROP COLUMN retry_after, "
        "DROP COLUMN attempt_count, "
        "DROP COLUMN bench_version"
    )

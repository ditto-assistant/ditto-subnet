"""Add public-safe validator ticket failure telemetry.

Revision ID: c4d7e8f9a0b1
Revises: f4b8a9137c2d
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "f4b8a9137c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validator_tickets", sa.Column("failure_reason", sa.Text(), nullable=True)
    )
    op.add_column(
        "validator_tickets",
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "validator_tickets_failure_reason",
        "validator_tickets",
        "failure_reason IS NULL OR failure_reason IN "
        "('infrastructure', 'scoring_error', 'sandbox_oom')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "validator_tickets_failure_reason",
        "validator_tickets",
        type_="check",
    )
    op.drop_column("validator_tickets", "failed_at")
    op.drop_column("validator_tickets", "failure_reason")

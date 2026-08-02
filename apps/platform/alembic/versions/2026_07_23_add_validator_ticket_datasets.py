"""add validator-specific dataset pins to scoring tickets

Revision ID: f4b8a9137c2d
Revises: c3a71f9d4e82
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4b8a9137c2d"
# Ticket dataset pins extend the ticket-purpose schema introduced immediately
# downstack; keeping this linear avoids two production Alembic heads.
down_revision: str | Sequence[str] | None = "c3a71f9d4e82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("validator_tickets", sa.Column("seed", sa.BigInteger()))
    op.add_column("validator_tickets", sa.Column("dataset_sha256", sa.Text()))
    op.add_column("validator_tickets", sa.Column("seed_block", sa.BigInteger()))
    op.add_column("validator_tickets", sa.Column("seed_block_hash", sa.Text()))
    op.create_check_constraint(
        "validator_tickets_seed_nonnegative",
        "validator_tickets",
        "seed IS NULL OR seed >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "validator_tickets_seed_nonnegative",
        "validator_tickets",
        type_="check",
    )
    op.drop_column("validator_tickets", "seed_block_hash")
    op.drop_column("validator_tickets", "seed_block")
    op.drop_column("validator_tickets", "dataset_sha256")
    op.drop_column("validator_tickets", "seed")

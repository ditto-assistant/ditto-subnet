"""Store bounded authenticated relay diagnostics independently of payout proof."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c74e5d2a931b"
down_revision = "b63d4c1f820a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "validator_receipt_diagnostics",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("signed_at", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("validator_receipt_diagnostics")

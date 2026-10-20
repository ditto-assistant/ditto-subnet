"""Add per-epoch Gamma collector ledger samples.

Revision ID: 3f1c9a7d2b6e
Revises: a4963e2b18c7
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "3f1c9a7d2b6e"
down_revision: str | None = "a4963e2b18c7"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "treasury_ledger_samples"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("block_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "sampled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("collector_hotkey", sa.Text(), nullable=False),
        sa.Column("collector_coldkey", sa.Text(), nullable=False),
        sa.Column("collector_uid", sa.Integer()),
        sa.Column("collector_incentive", sa.Float()),
        sa.Column("collector_emission_rao", sa.BigInteger()),
        sa.Column("collector_total_rao", sa.BigInteger(), nullable=False),
        sa.Column("collector_locked_rao", sa.BigInteger(), nullable=False),
        sa.Column("collector_available_rao", sa.BigInteger(), nullable=False),
        sa.Column("collector_lock_perpetual", sa.Boolean()),
        sa.Column("owner_hotkey", sa.Text()),
        sa.Column("owner_conviction_rao", sa.BigInteger()),
        sa.Column("most_convicted_hotkey", sa.Text()),
        sa.Column(
            "holdings",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.UniqueConstraint(
            "netuid", "epoch_index", name="treasury_ledger_samples_netuid_epoch"
        ),
        sa.CheckConstraint(
            "collector_total_rao >= 0 AND collector_locked_rao >= 0 "
            "AND collector_available_rao >= 0",
            name="treasury_ledger_samples_nonnegative",
        ),
    )


def downgrade() -> None:
    op.drop_table(TABLE)

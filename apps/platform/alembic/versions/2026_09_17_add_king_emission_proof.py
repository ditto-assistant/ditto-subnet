"""Require completed winner emission proof before source disclosure.

Revision ID: a54c2b0e719f
Revises: f2c8d41a6b90
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a54c2b0e719f"
down_revision = "f2c8d41a6b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately no backfill: revealed weights are not completed earnings.
    for column in (
        sa.Column("emission_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emission_block", sa.BigInteger(), nullable=True),
        sa.Column("emission_block_hash", sa.Text(), nullable=True),
        sa.Column("emission_epoch_index", sa.BigInteger(), nullable=True),
        sa.Column("emission_ledger_digest", sa.Text(), nullable=True),
        sa.Column("emission_evidence", postgresql.JSONB(), nullable=True),
    ):
        op.add_column("agent_kingship", column)

    op.create_table(
        "validator_weights_fold_history",
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("fold_digest", sa.Text(), primary_key=True),
        sa.Column("folded_at", sa.BigInteger(), nullable=False),
        sa.Column("vector_digest", sa.Text(), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=True),
        sa.Column("ledger_digest", sa.Text(), nullable=True),
        sa.Column("champion_agent_id", sa.Uuid(), nullable=True),
        sa.Column("weights_fold", postgresql.JSONB(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("signed_heartbeat", postgresql.JSONB(), nullable=True),
    )

    op.create_index(
        "validator_weights_fold_history_lookup_idx",
        "validator_weights_fold_history",
        ["validator_hotkey", "folded_at"],
    )


def downgrade() -> None:
    op.drop_table("validator_weights_fold_history")
    for name in (
        "emission_evidence",
        "emission_ledger_digest",
        "emission_epoch_index",
        "emission_block_hash",
        "emission_block",
        "emission_confirmed_at",
    ):
        op.drop_column("agent_kingship", name)

"""Add generator frontier disputes: the deposit and the refund.

A column owner disputes an oracle-failed case with a TAO deposit
(generator_frontier, "Disputes"). The dispute itself lives in the round's
frontier document with the rest of the lane; this table holds the money. Its
primary key on the payment proof stops one transfer funding two disputes,
and the payment lanes refuse a proof recorded here, so a transfer pays for
exactly one thing. An upheld dispute (or one left unruled) records a refund
as an operator payout row: Platform holds no key that could send it. A new
table, no hot table touched, no live behavior changed.

Revision ID: dce966642b0d
Revises: 5c2b75e9c0b4
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dce966642b0d"
down_revision: str | Sequence[str] | None = "5c2b75e9c0b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generator_frontier_disputes",
        sa.Column("dispute_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("amount_rao", sa.BigInteger(), nullable=False),
        sa.Column("tao_usd_rate", sa.Numeric(20, 8), nullable=True),
        sa.Column("dest_address", sa.Text(), nullable=False),
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("refund_rao", sa.BigInteger(), nullable=True),
        sa.Column("refund_status", sa.Text(), nullable=False, server_default="none"),
        sa.Column("refund_block_hash", sa.Text(), nullable=True),
        sa.Column("refund_extrinsic_index", sa.Integer(), nullable=True),
        sa.Column("refund_paid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("refund_actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "block_hash", "extrinsic_index", name="generator_frontier_disputes_pkey"
        ),
        sa.UniqueConstraint(
            "dispute_id", name="generator_frontier_disputes_dispute_id_key"
        ),
        sa.UniqueConstraint(
            "round_index", "case_id", name="generator_frontier_disputes_case_key"
        ),
        sa.CheckConstraint(
            "block_hash = lower(block_hash)",
            name="generator_frontier_disputes_block_hash_check",
        ),
        sa.CheckConstraint(
            "extrinsic_index >= 0",
            name="generator_frontier_disputes_extrinsic_index_check",
        ),
        sa.CheckConstraint(
            "amount_rao > 0", name="generator_frontier_disputes_amount_rao_check"
        ),
        sa.CheckConstraint(
            "refund_status IN ('none', 'pending', 'paid')",
            name="generator_frontier_disputes_refund_status_check",
        ),
        sa.CheckConstraint(
            "(refund_status = 'none') = (refund_rao IS NULL)",
            name="generator_frontier_disputes_refund_amount_check",
        ),
        sa.CheckConstraint(
            "refund_rao IS NULL OR (refund_rao > 0 AND refund_rao <= amount_rao)",
            name="generator_frontier_disputes_refund_bound_check",
        ),
        sa.CheckConstraint(
            "(refund_status = 'paid') = (refund_block_hash IS NOT NULL)",
            name="generator_frontier_disputes_refund_paid_check",
        ),
    )
    op.create_index(
        "generator_frontier_disputes_refund_idx",
        "generator_frontier_disputes",
        ["refund_status", "created_at"],
    )
    # One refund transfer pays one dispute.
    op.create_index(
        "generator_frontier_disputes_refund_transfer_key",
        "generator_frontier_disputes",
        ["refund_block_hash", "refund_extrinsic_index"],
        unique=True,
        postgresql_where=sa.text("refund_block_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("generator_frontier_disputes")

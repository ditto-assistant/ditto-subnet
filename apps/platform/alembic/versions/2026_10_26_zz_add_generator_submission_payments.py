"""Record the TAO fee that funds a generator upload.

An optional generator-competition setting (``upload_fee_required``, off by
default) makes every generator upload carry a finalized payment proof, priced
and addressed by the same submission settings as agent uploads. One row binds
one on-chain transfer to the one generator submission it funded. The primary
key on ``(block_hash, extrinsic_index)`` stops a proof funding two generator
uploads; the upload paths also refuse a proof already recorded in
``evaluation_payments`` (and agent uploads refuse one recorded here) under a
shared per-proof advisory lock, so a transfer funds exactly one upload across
both lanes.

The table is new and empty, so a plain create takes no lock on a hot table.

Revision ID: 9e4b2d7c1a63
Revises: 9d1b6a3f5c28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e4b2d7c1a63"
down_revision: str | Sequence[str] | None = "9d1b6a3f5c28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generator_submission_payments",
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("amount_rao", sa.BigInteger(), nullable=False),
        sa.Column("tao_usd_rate", sa.Numeric(20, 8), nullable=True),
        sa.Column("dest_address", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "block_hash",
            "extrinsic_index",
            name="generator_submission_payments_pkey",
        ),
        sa.UniqueConstraint(
            "submission_id", name="generator_submission_payments_submission_key"
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["generator_submissions.submission_id"],
            ondelete="RESTRICT",
            name="generator_submission_payments_submission_fkey",
        ),
        sa.CheckConstraint(
            "block_hash = lower(block_hash)",
            name="generator_submission_payments_block_hash_check",
        ),
        sa.CheckConstraint(
            "extrinsic_index >= 0",
            name="generator_submission_payments_extrinsic_index_check",
        ),
        sa.CheckConstraint(
            "amount_rao > 0", name="generator_submission_payments_amount_rao_check"
        ),
        sa.CheckConstraint(
            "tao_usd_rate IS NULL OR tao_usd_rate > 0",
            name="generator_submission_payments_tao_usd_rate_check",
        ),
    )
    op.create_index(
        "generator_submission_payments_miner_coldkey_idx",
        "generator_submission_payments",
        ["miner_coldkey"],
    )


def downgrade() -> None:
    op.drop_index(
        "generator_submission_payments_miner_coldkey_idx",
        table_name="generator_submission_payments",
    )
    op.drop_table("generator_submission_payments")

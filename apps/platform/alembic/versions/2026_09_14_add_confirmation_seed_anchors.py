"""Bind each bench v13+ confirmation seed family to a finalized chain block.

One row per ``(champion, bench_version)`` reign. Created at the reign's first
continual-retest claim with the hash still null (the finality wait for
``ready_block + Δ``), pinned exactly once, never re-pinned. Legacy versions get
no row and keep the unbound champion-anchored derivation byte-for-byte.

Revision ID: 9e4b2f7c1a53
Revises: 5e1f7a9c2b04
"""

import sqlalchemy as sa

from alembic import op

revision = "9e4b2f7c1a53"
down_revision = "5e1f7a9c2b04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "confirmation_seed_anchors",
        sa.Column("champion_agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("ready_block", sa.BigInteger(), nullable=False),
        sa.Column("anchor_block", sa.BigInteger(), nullable=False),
        sa.Column("anchor_block_hash", sa.Text(), nullable=True),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "champion_agent_id",
            "bench_version",
            name="confirmation_seed_anchors_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["champion_agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="confirmation_seed_anchors_champion_fkey",
        ),
        sa.CheckConstraint(
            "bench_version > 0",
            name="confirmation_seed_anchors_bench_version_positive",
        ),
        sa.CheckConstraint(
            "ready_block >= 0 AND anchor_block > ready_block",
            name="confirmation_seed_anchors_block_order_check",
        ),
        sa.CheckConstraint(
            "anchor_block_hash IS NULL OR anchor_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="confirmation_seed_anchors_hash_check",
        ),
        sa.CheckConstraint(
            "(anchor_block_hash IS NULL) = (pinned_at IS NULL)",
            name="confirmation_seed_anchors_pin_check",
        ),
    )
    op.create_index(
        "confirmation_seed_anchors_version_idx",
        "confirmation_seed_anchors",
        ["bench_version", "anchor_block"],
    )


def downgrade() -> None:
    op.drop_index(
        "confirmation_seed_anchors_version_idx",
        table_name="confirmation_seed_anchors",
    )
    op.drop_table("confirmation_seed_anchors")

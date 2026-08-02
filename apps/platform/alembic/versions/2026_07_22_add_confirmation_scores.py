"""Add the append-only top-5 confirmation-score ledger.

Revision ID: f1a2c3d4e5b6
Revises: c7a91f04d2be
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2c3d4e5b6"
down_revision: str | Sequence[str] | None = "c7a91f04d2be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confirmation_scores",
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "seed",
            name="confirmation_scores_pkey",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="confirmation_scores_agent_id_fkey",
        ),
        sa.CheckConstraint(
            "composite >= 0 AND composite <= 1",
            name="confirmation_scores_composite_range_check",
        ),
        sa.CheckConstraint(
            "bench_version > 0", name="confirmation_scores_bench_version_positive"
        ),
        sa.CheckConstraint("seed >= 0", name="confirmation_scores_seed_check"),
    )
    op.create_index(
        "confirmation_scores_agent_version_idx",
        "confirmation_scores",
        ["agent_id", "bench_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "confirmation_scores_agent_version_idx", table_name="confirmation_scores"
    )
    op.drop_table("confirmation_scores")

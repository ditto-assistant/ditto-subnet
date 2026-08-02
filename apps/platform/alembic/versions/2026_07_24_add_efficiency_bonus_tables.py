"""add frozen relative token-efficiency cohort snapshots and bonuses

Revision ID: d5f1a8c62b93
Revises: d91f3a7b2c10
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5f1a8c62b93"
down_revision: str | Sequence[str] | None = "d91f3a7b2c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "efficiency_cohort_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("run_size", sa.Text(), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("cohort_limit", sa.Integer(), nullable=False),
        sa.Column("n_min", sa.Integer(), nullable=False),
        sa.Column("bonus_cap", sa.Float(), nullable=False),
        sa.Column(
            "curve_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("deep_bonus_cap", sa.Float(), nullable=True),
        sa.Column("deep_frontier_ratio", sa.Float(), nullable=True),
        sa.Column("quality_floor", sa.Float(), nullable=False),
        sa.Column("memory_floor", sa.Float(), nullable=False),
        sa.Column("reference_p25_tokens", sa.Float(), nullable=True),
        sa.Column("reference_median_tokens", sa.Float(), nullable=True),
        sa.Column("members", json_type, nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "bench_version",
            "run_size",
            "epoch_index",
            name="efficiency_cohort_snapshots_epoch_key",
        ),
        sa.CheckConstraint(
            "bench_version >= 7",
            name="efficiency_cohort_snapshots_bench_version_check",
        ),
        sa.CheckConstraint(
            "bonus_cap > 0 AND bonus_cap <= 0.1",
            name="efficiency_cohort_snapshots_cap_check",
        ),
        sa.CheckConstraint(
            "deep_bonus_cap IS NULL OR "
            "(deep_bonus_cap >= bonus_cap AND deep_bonus_cap <= 0.1)",
            name="efficiency_cohort_snapshots_deep_cap_check",
        ),
        sa.CheckConstraint(
            "deep_frontier_ratio IS NULL OR "
            "(deep_frontier_ratio > 0 AND deep_frontier_ratio < 1)",
            name="efficiency_cohort_snapshots_deep_frontier_check",
        ),
        sa.CheckConstraint(
            "curve_version >= 1",
            name="efficiency_cohort_snapshots_curve_version_check",
        ),
        sa.CheckConstraint(
            "n_min >= 2", name="efficiency_cohort_snapshots_n_min_check"
        ),
    )
    op.create_index(
        "efficiency_cohort_snapshots_board_idx",
        "efficiency_cohort_snapshots",
        ["bench_version", "run_size", "epoch_index"],
    )
    op.create_table(
        "efficiency_bonuses",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("token_total", sa.Float(), nullable=True),
        sa.Column("bonus", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "agent_id", "bench_version", name="efficiency_bonuses_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="efficiency_bonuses_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["efficiency_cohort_snapshots.snapshot_id"],
            name="efficiency_bonuses_snapshot_id_fkey",
        ),
        sa.CheckConstraint(
            "bonus >= 0 AND bonus <= 0.1",
            name="efficiency_bonuses_bonus_range_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 7", name="efficiency_bonuses_bench_version_check"
        ),
    )
    op.create_index(
        "efficiency_bonuses_snapshot_idx", "efficiency_bonuses", ["snapshot_id"]
    )


def downgrade() -> None:
    op.drop_index("efficiency_bonuses_snapshot_idx", table_name="efficiency_bonuses")
    op.drop_table("efficiency_bonuses")
    op.drop_index(
        "efficiency_cohort_snapshots_board_idx",
        table_name="efficiency_cohort_snapshots",
    )
    op.drop_table("efficiency_cohort_snapshots")

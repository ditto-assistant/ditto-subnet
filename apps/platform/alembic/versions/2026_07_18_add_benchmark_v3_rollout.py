"""add durable benchmark v3 activation cohort

Revision ID: b8e4a91c2f30
Revises: f7c8d9e0a1b2
Create Date: 2026-07-18 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8e4a91c2f30"
down_revision: str | Sequence[str] | None = "f7c8d9e0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    with op.batch_alter_table("scores") as batch:
        batch.add_column(
            sa.Column("bench_version", sa.Integer(), server_default="2", nullable=False)
        )
        batch.drop_constraint("scores_pkey", type_="primary")
        batch.create_primary_key(
            "scores_pkey", ["agent_id", "bench_version", "validator_hotkey"]
        )
        batch.create_check_constraint(
            "scores_bench_version_positive", "bench_version > 0"
        )
    with op.batch_alter_table("validator_tickets") as batch:
        batch.drop_constraint("validator_tickets_pkey", type_="primary")
        batch.create_primary_key(
            "validator_tickets_pkey",
            ["agent_id", "bench_version", "validator_hotkey"],
        )
    op.create_table(
        "benchmark_datasets",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("run_size", sa.Text(), nullable=False),
        sa.Column("seed_block", sa.BigInteger(), nullable=True),
        sa.Column("seed_block_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bench_version > 0", name="benchmark_dataset_version_positive"
        ),
        sa.CheckConstraint("length(sha256) = 64", name="benchmark_dataset_sha_length"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "agent_id", "bench_version", name="benchmark_datasets_pkey"
        ),
    )
    op.execute(
        """
        INSERT INTO benchmark_datasets
            (agent_id, bench_version, seed, sha256, run_size,
             seed_block, seed_block_hash)
        SELECT agent_id, 2, dataset_seed, dataset_sha256, dataset_run_size,
               dataset_seed_block, dataset_seed_block_hash
          FROM agents
         WHERE dataset_seed IS NOT NULL
           AND dataset_sha256 IS NOT NULL
           AND dataset_run_size IS NOT NULL
        """
    )
    op.create_table(
        "benchmark_rollouts",
        sa.Column("rollout_id", sa.Uuid(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("desired_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("cohort_size", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("from_version > 0", name="benchmark_rollout_from_positive"),
        sa.CheckConstraint(
            "desired_version > from_version", name="benchmark_rollout_forward"
        ),
        sa.CheckConstraint("cohort_size = 5", name="benchmark_rollout_five_members"),
        sa.CheckConstraint(
            "status IN ('collecting', 'blocked_ineligible', 'activated')",
            name="benchmark_rollout_status",
        ),
        sa.PrimaryKeyConstraint("rollout_id"),
    )
    op.create_index(
        "benchmark_rollouts_one_open_idx",
        "benchmark_rollouts",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status IN ('collecting', 'blocked_ineligible')"),
        sqlite_where=sa.text("status IN ('collecting', 'blocked_ineligible')"),
    )
    op.create_table(
        "benchmark_rollout_members",
        sa.Column("rollout_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("frozen_miner_hotkey", sa.Text(), nullable=False),
        sa.Column("frozen_composite", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 5", name="benchmark_member_position"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["benchmark_rollouts.rollout_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("rollout_id", "agent_id"),
        sa.UniqueConstraint("rollout_id", "position"),
    )
    op.create_table(
        "benchmark_rollout_audit",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("rollout_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["rollout_id"], ["benchmark_rollouts.rollout_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "benchmark_rollout_audit_history_idx",
        "benchmark_rollout_audit",
        ["rollout_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "benchmark_rollout_audit_history_idx", table_name="benchmark_rollout_audit"
    )
    op.drop_table("benchmark_rollout_audit")
    op.drop_table("benchmark_rollout_members")
    op.drop_index("benchmark_rollouts_one_open_idx", table_name="benchmark_rollouts")
    op.drop_table("benchmark_rollouts")
    op.drop_table("benchmark_datasets")
    op.execute("DELETE FROM scores WHERE bench_version <> 2")
    op.execute("DELETE FROM validator_tickets WHERE bench_version <> 2")
    with op.batch_alter_table("validator_tickets") as batch:
        batch.drop_constraint("validator_tickets_pkey", type_="primary")
        batch.create_primary_key(
            "validator_tickets_pkey", ["agent_id", "validator_hotkey"]
        )
    with op.batch_alter_table("scores") as batch:
        batch.drop_constraint("scores_pkey", type_="primary")
        batch.create_primary_key("scores_pkey", ["agent_id", "validator_hotkey"])
        batch.drop_constraint("scores_bench_version_positive", type_="check")
        batch.drop_column("bench_version")

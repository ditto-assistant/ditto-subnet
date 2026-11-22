"""Add per-run harness memory observations (SN-500 step 1).

One row per scored or failed lease: the scorer's peak and time-weighted
P50/P95 of the harness cgroup's memory, reported unsigned by the validator.
Operator observability only; nothing reads it for policy and nothing
publishes it. A new table, no hot table touched, no live behavior changed.

Revision ID: 4f8d2b6c1e37
Revises: 5c2b75e9c0b4
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4f8d2b6c1e37"
down_revision: str | Sequence[str] | None = "5c2b75e9c0b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "harness_memory_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("lease_issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("peak_bytes", sa.BigInteger(), nullable=False),
        sa.Column("p50_bytes", sa.BigInteger(), nullable=False),
        sa.Column("p95_bytes", sa.BigInteger(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("sampled_seconds", sa.Float(), nullable=False),
        sa.Column("limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="harness_memory_observations_agent_fkey",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "lease_issued_at",
            name="harness_memory_observations_lease_key",
        ),
        sa.CheckConstraint(
            "outcome IN ('scored', 'failed')",
            name="outcome",
        ),
        sa.CheckConstraint(
            "peak_bytes >= 0 AND p50_bytes >= 0 AND p95_bytes >= 0 "
            "AND samples >= 0 AND sampled_seconds >= 0",
            name="nonnegative",
        ),
    )
    op.create_index(
        "harness_memory_observations_agent_idx",
        "harness_memory_observations",
        ["agent_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "harness_memory_observations_agent_idx",
        table_name="harness_memory_observations",
    )
    op.drop_table("harness_memory_observations")

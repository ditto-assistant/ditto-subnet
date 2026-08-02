"""add audited validator queue withdrawals

Revision ID: b7e6d5c4a3f2
Revises: a6d5c4b3e2f1
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e6d5c4a3f2"
down_revision: str | None = "a6d5c4b3e2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validator_queue_withdrawals",
        sa.Column("withdrawal_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_snapshot", sa.Text(), nullable=False),
        sa.Column("score_count", sa.Integer(), nullable=False),
        sa.Column(
            "ticket_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("withdrawal_id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="validator_queue_withdrawals_agent_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "bench_version > 0",
            name="validator_queue_withdrawals_bench_version_positive",
        ),
        sa.CheckConstraint(
            "score_count >= 0",
            name="validator_queue_withdrawals_score_count_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_queue_withdrawals_actor_length",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="validator_queue_withdrawals_reason_length",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            name="validator_queue_withdrawals_agent_bench_key",
        ),
    )
    op.create_index(
        "validator_queue_withdrawals_created_idx",
        "validator_queue_withdrawals",
        ["created_at", "withdrawal_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "validator_queue_withdrawals_created_idx",
        table_name="validator_queue_withdrawals",
    )
    op.drop_table("validator_queue_withdrawals")

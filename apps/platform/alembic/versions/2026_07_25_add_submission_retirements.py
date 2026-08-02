"""add audited previous-generation submission retirements

Revision ID: f3b8c2d17a49
Revises: d7b3e5c81a94
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3b8c2d17a49"
down_revision: str | None = "d7b3e5c81a94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_retirements",
        sa.Column("retirement_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("superseded_by_version", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("retirement_id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="submission_retirements_agent_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "bench_version > 0",
            name="submission_retirements_bench_version_positive",
        ),
        sa.CheckConstraint(
            "superseded_by_version > bench_version",
            name="submission_retirements_superseded_by_is_newer",
        ),
        sa.CheckConstraint(
            "score_count >= 0",
            name="submission_retirements_score_count_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_retirements_actor_length",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="submission_retirements_reason_length",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            name="submission_retirements_agent_bench_key",
        ),
    )
    op.create_index(
        "submission_retirements_created_idx",
        "submission_retirements",
        ["created_at", "retirement_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "submission_retirements_created_idx",
        table_name="submission_retirements",
    )
    op.drop_table("submission_retirements")

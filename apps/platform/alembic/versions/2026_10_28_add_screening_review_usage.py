"""Per-attempt screening review model usage telemetry.

Revision ID: 5e2b9d7c4a18
Revises: 5b8e2d4f6a13
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5e2b9d7c4a18"
down_revision = "5b8e2d4f6a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "screening_review_usage",
        sa.Column("attempt_id", sa.UUID(), primary_key=True),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("screener_hotkey", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("stages", postgresql.JSONB(), nullable=False),
        sa.Column("total_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("elapsed_ms", sa.BigInteger(), nullable=True),
        sa.Column("lineage_scope", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="screening_review_usage_attempt_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="screening_review_usage_agent_id_fkey",
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64",
            name="screening_review_usage_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "total_input_tokens >= 0 AND total_output_tokens >= 0 "
            "AND total_cost_usd >= 0 AND (elapsed_ms IS NULL OR elapsed_ms >= 0)",
            name="screening_review_usage_totals_check",
        ),
        sa.CheckConstraint(
            "lineage_scope IS NULL OR lineage_scope IN ('diff', 'full')",
            name="screening_review_usage_lineage_scope_check",
        ),
    )
    op.create_index(
        "screening_review_usage_recorded_idx",
        "screening_review_usage",
        ["recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screening_review_usage_recorded_idx", table_name="screening_review_usage"
    )
    op.drop_table("screening_review_usage")

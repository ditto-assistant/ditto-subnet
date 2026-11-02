"""Durable, budget-reserved shadow conversation assessments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "ae67182d095c"
down_revision = "9ce14a73b206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_assessments",
        sa.Column("assessment_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("agents.agent_id"), nullable=False
        ),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("instrument", sa.Text(), nullable=False),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_quality_micros", sa.Integer(), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=True),
        sa.Column("worker_hotkey", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "agent_id",
            "artifact_sha256",
            "bench_version",
            "instrument",
            name="conversation_assessment_identity",
        ),
        sa.CheckConstraint(
            "base_quality_micros BETWEEN 0 AND 1000000",
            name="conversation_base_quality",
        ),
        sa.CheckConstraint(
            "reserved_microusd = 30000000", name="conversation_reserved_cost"
        ),
        sa.CheckConstraint("bench_version >= 9", name="conversation_bench_version"),
    )
    op.create_index(
        "conversation_created_at", "conversation_assessments", ["created_at"]
    )
    op.create_table(
        "conversation_settings_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_settings_revisions")
    op.drop_table("conversation_assessments")

"""add report-only screener probe batteries

Revision ID: 5f2c8e1a9d47
Revises: 73bd92c14f60
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5f2c8e1a9d47"
down_revision: str | Sequence[str] | None = "73bd92c14f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "screener_probe_batteries",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("screener_hotkey", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("settings_scope", sa.Text(), nullable=False),
        sa.Column("settings_checksum", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("encoding", sa.Text(), nullable=True),
        sa.Column("seed_first", sa.Boolean(), nullable=False),
        sa.Column("items_run", sa.Integer(), nullable=False),
        sa.Column("probe_count", sa.Integer(), nullable=False),
        sa.Column("leads", json_type, nullable=False),
        sa.Column("battery_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64",
            name="screener_probe_batteries_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'calibration_failed', 'timeout', 'error')",
            name="screener_probe_batteries_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            name="screener_probe_batteries_attempt_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="screener_probe_batteries_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision"],
            ["screener_review_settings_revisions.revision"],
            name="screener_probe_batteries_settings_revision_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        "screener_probe_batteries_created_idx",
        "screener_probe_batteries",
        ["created_at"],
    )
    op.create_index(
        "screener_probe_batteries_agent_idx",
        "screener_probe_batteries",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screener_probe_batteries_agent_idx", table_name="screener_probe_batteries"
    )
    op.drop_index(
        "screener_probe_batteries_created_idx", table_name="screener_probe_batteries"
    )
    op.drop_table("screener_probe_batteries")

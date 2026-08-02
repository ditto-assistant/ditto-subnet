"""add attempt-bound screener shadow reviews

Revision ID: c7d2a10f4e9b
Revises: b4c9e2f71a08
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7d2a10f4e9b"
down_revision: str | Sequence[str] | None = "b4c9e2f71a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "screener_shadow_reviews",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("screener_hotkey", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("settings_scope", sa.Text(), nullable=False),
        sa.Column("settings_checksum", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=True),
        sa.Column("categories", json_type, nullable=False),
        sa.Column("finding_digest", sa.Text(), nullable=True),
        sa.Column("resolution_basis", sa.Text(), nullable=True),
        sa.Column("clearance_path", sa.Text(), nullable=True),
        sa.Column("critic_disposition", sa.Text(), nullable=True),
        sa.Column("adjudicator_disposition", sa.Text(), nullable=True),
        sa.Column("response_models", json_type, nullable=False),
        sa.Column("response_providers", json_type, nullable=False),
        sa.Column("usage", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64",
            name="screener_shadow_reviews_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "length(settings_checksum) = 64",
            name="screener_shadow_reviews_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "disposition IN ('safe', 'violation', 'inconclusive', 'retryable_infra')",
            name="screener_shadow_reviews_disposition_check",
        ),
        sa.CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')",
            name="screener_shadow_reviews_risk_check",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            name="screener_shadow_reviews_attempt_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="screener_shadow_reviews_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision"],
            ["screener_review_settings_revisions.revision"],
            name="screener_shadow_reviews_settings_revision_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        "screener_shadow_reviews_created_idx",
        "screener_shadow_reviews",
        ["created_at"],
    )
    op.create_index(
        "screener_shadow_reviews_agent_idx",
        "screener_shadow_reviews",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screener_shadow_reviews_agent_idx", table_name="screener_shadow_reviews"
    )
    op.drop_index(
        "screener_shadow_reviews_created_idx", table_name="screener_shadow_reviews"
    )
    op.drop_table("screener_shadow_reviews")

"""add trusted image build queue

Revision ID: c8a2f491e7d3
Revises: f68d2c1a9b04
Create Date: 2026-08-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8a2f491e7d3"
down_revision: str | Sequence[str] | None = "f68d2c1a9b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trusted_image_builds",
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("source_repository", sa.Text(), nullable=False),
        sa.Column("source_sha", sa.Text(), nullable=False),
        sa.Column("context_path", sa.Text(), nullable=False),
        sa.Column("dockerfile_path", sa.Text(), nullable=False),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("provider_resource_id", sa.Text(), nullable=True),
        sa.Column("image_digest", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("controller_epoch", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="trusted_image_builds_environment_check",
        ),
        sa.CheckConstraint(
            "component IN ('screener')", name="trusted_image_builds_component_check"
        ),
        sa.CheckConstraint(
            "source_sha ~ '^[0-9a-f]{40}$'",
            name="trusted_image_builds_source_sha_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', 'failed', "
            "'fallback_required', 'canceled')",
            name="trusted_image_builds_status_check",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider IN ('targon', 'gcp')",
            name="trusted_image_builds_provider_check",
        ),
        sa.CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="trusted_image_builds_digest_check",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 10",
            name="trusted_image_builds_attempt_count_check",
        ),
        sa.PrimaryKeyConstraint("build_id"),
        sa.UniqueConstraint(
            "environment",
            "component",
            "source_sha",
            name="trusted_image_builds_source_key",
        ),
    )
    op.create_index(
        "trusted_image_builds_queue_idx",
        "trusted_image_builds",
        ["environment", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("trusted_image_builds_queue_idx", table_name="trusted_image_builds")
    op.drop_table("trusted_image_builds")

"""add attempt-bound remote submission image builds

Revision ID: d19a7e4b2c11
Revises: b4d9e7c2a601
Create Date: 2026-08-11 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d19a7e4b2c11"
down_revision: str | Sequence[str] | None = "b4d9e7c2a601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_image_builds",
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("image_ref", sa.Text(), nullable=False),
        sa.Column("output_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("provider_resource_id", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.Text(), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("controller_epoch", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("job_token_hash", sa.Text(), nullable=True),
        sa.Column("job_token_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("upload_minted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="submission_image_builds_environment_check",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "image_ref = 'ditto-screen/' || agent_id::text || '-' || "
            "attempt_id::text || chr(58) || 'latest'",
            name="submission_image_builds_image_ref_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', "
            "'fallback_required', 'canceled', 'consumed')",
            name="submission_image_builds_status_check",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider = 'targon'",
            name="submission_image_builds_provider_check",
        ),
        sa.CheckConstraint(
            "output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_output_sha_check",
        ),
        sa.CheckConstraint(
            "output_size_bytes IS NULL OR output_size_bytes BETWEEN 1 AND 4294967296",
            name="submission_image_builds_output_size_check",
        ),
        sa.CheckConstraint(
            "job_token_hash IS NULL OR job_token_hash ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_token_hash_check",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="submission_image_builds_attempt_count_check",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="submission_image_builds_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            name="submission_image_builds_attempt_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("build_id"),
        sa.UniqueConstraint("attempt_id", name="submission_image_builds_attempt_key"),
    )
    op.create_index(
        "submission_image_builds_queue_idx",
        "submission_image_builds",
        ["environment", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "submission_image_builds_queue_idx", table_name="submission_image_builds"
    )
    op.drop_table("submission_image_builds")

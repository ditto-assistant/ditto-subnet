"""add audited screener provider routing settings

Revision ID: 8a31d2c7f4be
Revises: f1a7b3c9d502
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8a31d2c7f4be"
down_revision: str | None = "f1a7b3c9d502"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_provider_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column(
            "settings",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_provider_settings_environment_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="screener_provider_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_provider_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_provider_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "environment",
            "parent_revision",
            name="screener_provider_settings_environment_parent_key",
        ),
    )
    op.create_index(
        "screener_provider_settings_environment_revision_idx",
        "screener_provider_settings_revisions",
        ["environment", "revision"],
        unique=True,
    )
    op.add_column(
        "screener_capacity_snapshots",
        sa.Column(
            "provider_settings_revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "screener_capacity_snapshots_provider_revision_check",
        "screener_capacity_snapshots",
        "provider_settings_revision >= 0",
    )
    op.add_column(
        "submission_image_builds",
        sa.Column(
            "runtime_status", sa.Text(), server_default="skipped", nullable=False
        ),
    )
    op.add_column(
        "submission_image_builds",
        sa.Column("runtime_provider_resource_id", sa.Text()),
    )
    op.add_column(
        "submission_image_builds",
        sa.Column("runtime_image_reference", sa.Text()),
    )
    op.add_column(
        "submission_image_builds",
        sa.Column("runtime_error_code", sa.Text()),
    )
    op.add_column(
        "submission_image_builds",
        sa.Column("runtime_completed_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_check_constraint(
        "submission_image_builds_runtime_status_check",
        "submission_image_builds",
        "runtime_status IN ('pending', 'running', 'succeeded', "
        "'fallback_required', 'skipped')",
    )
    op.create_check_constraint(
        "submission_image_builds_runtime_image_check",
        "submission_image_builds",
        "runtime_image_reference IS NULL OR runtime_image_reference ~ "
        "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
    )
    op.create_table(
        "submission_source_reviews",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("provider", sa.Text()),
        sa.Column("provider_resource_id", sa.Text()),
        sa.Column(
            "observation",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
        ),
        sa.Column("error_code", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("controller_epoch", sa.Text()),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("job_token_hash", sa.Text()),
        sa.Column("job_token_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="submission_source_reviews_environment_check",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_source_reviews_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', "
            "'fallback_required', 'canceled', 'consumed')",
            name="submission_source_reviews_status_check",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider = 'targon'",
            name="submission_source_reviews_provider_check",
        ),
        sa.CheckConstraint(
            "job_token_hash IS NULL OR job_token_hash ~ '^[0-9a-f]{64}$'",
            name="submission_source_reviews_token_hash_check",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="submission_source_reviews_attempt_count_check",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="submission_source_reviews_agent_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="submission_source_reviews_attempt_id_fkey",
        ),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("attempt_id", name="submission_source_reviews_attempt_key"),
    )
    op.create_index(
        "submission_source_reviews_queue_idx",
        "submission_source_reviews",
        ["environment", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "submission_source_reviews_queue_idx",
        table_name="submission_source_reviews",
    )
    op.drop_table("submission_source_reviews")
    op.drop_constraint(
        "submission_image_builds_runtime_image_check",
        "submission_image_builds",
        type_="check",
    )
    op.drop_constraint(
        "submission_image_builds_runtime_status_check",
        "submission_image_builds",
        type_="check",
    )
    op.drop_column("submission_image_builds", "runtime_completed_at")
    op.drop_column("submission_image_builds", "runtime_error_code")
    op.drop_column("submission_image_builds", "runtime_image_reference")
    op.drop_column("submission_image_builds", "runtime_provider_resource_id")
    op.drop_column("submission_image_builds", "runtime_status")
    op.drop_constraint(
        "screener_capacity_snapshots_provider_revision_check",
        "screener_capacity_snapshots",
        type_="check",
    )
    op.drop_column("screener_capacity_snapshots", "provider_settings_revision")
    op.drop_index(
        "screener_provider_settings_environment_revision_idx",
        table_name="screener_provider_settings_revisions",
    )
    op.drop_table("screener_provider_settings_revisions")

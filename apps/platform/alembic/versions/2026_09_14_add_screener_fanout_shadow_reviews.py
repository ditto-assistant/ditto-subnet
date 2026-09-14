"""add durable screener fanout shadow reviews

Revision ID: e6f4a9c2d781
Revises: a91c3f5e7d24
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6f4a9c2d781"
down_revision: str | Sequence[str] | None = "a91c3f5e7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_fanout_shadow_reviews",
        sa.Column("shadow_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_manifest_profile", sa.Text(), nullable=False),
        sa.Column("policy_manifest_rotation_id", sa.Text(), nullable=False),
        sa.Column("policy_manifest_digest", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("settings_scope", sa.Text(), nullable=False),
        sa.Column("settings_checksum", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("outcome", sa.Text()),
        sa.Column("baseline", postgresql.JSONB(), nullable=False),
        sa.Column("report", postgresql.JSONB()),
        sa.Column("disagrees_with_baseline", sa.Boolean()),
        sa.Column("coverage_complete", sa.Boolean()),
        sa.Column("error_code", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("provider_resource_id", sa.Text()),
        sa.Column("controller_epoch", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("job_token_hash", sa.Text()),
        sa.Column("job_token_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "reserved_cost_microusd",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("reported_cost_microusd", sa.BigInteger()),
        sa.Column("reserved_at", sa.DateTime(timezone=True)),
        sa.Column("unmetered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision"],
            ["screener_review_settings_revisions.revision"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "attempt_id", name="screener_fanout_shadow_reviews_attempt_key"
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_fanout_shadow_reviews_environment_check",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="screener_fanout_shadow_reviews_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "policy_manifest_profile IN ('core', 'l1', 'l1_l2')",
            name="screener_fanout_shadow_reviews_manifest_profile_check",
        ),
        sa.CheckConstraint(
            "policy_manifest_digest ~ '^[0-9a-f]{64}$'",
            name="screener_fanout_shadow_reviews_manifest_digest_check",
        ),
        sa.CheckConstraint(
            "settings_checksum ~ '^[0-9a-f]{64}$'",
            name="screener_fanout_shadow_reviews_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', "
            "'incomplete', 'skipped')",
            name="screener_fanout_shadow_reviews_status_check",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('no_findings', 'candidate', "
            "'unresolved_candidate', 'critic_also_flagged', 'incomplete', 'skipped')",
            name="screener_fanout_shadow_reviews_outcome_check",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider IN ('targon', 'gcp')",
            name="screener_fanout_shadow_reviews_provider_check",
        ),
        sa.CheckConstraint(
            "job_token_hash IS NULL OR job_token_hash ~ '^[0-9a-f]{64}$'",
            name="screener_fanout_shadow_reviews_token_hash_check",
        ),
        sa.CheckConstraint(
            "reserved_cost_microusd >= 0 AND reported_cost_microusd >= 0",
            name="screener_fanout_shadow_reviews_cost_check",
        ),
    )
    op.create_index(
        "screener_fanout_shadow_reviews_queue_idx",
        "screener_fanout_shadow_reviews",
        ["environment", "status", "created_at"],
    )
    op.create_index(
        "screener_fanout_shadow_reviews_created_idx",
        "screener_fanout_shadow_reviews",
        ["created_at", "shadow_id"],
    )
    op.create_index(
        "screener_fanout_shadow_reviews_reserved_idx",
        "screener_fanout_shadow_reviews",
        ["reserved_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screener_fanout_shadow_reviews_reserved_idx",
        table_name="screener_fanout_shadow_reviews",
    )
    op.drop_index(
        "screener_fanout_shadow_reviews_created_idx",
        table_name="screener_fanout_shadow_reviews",
    )
    op.drop_index(
        "screener_fanout_shadow_reviews_queue_idx",
        table_name="screener_fanout_shadow_reviews",
    )
    op.drop_table("screener_fanout_shadow_reviews")

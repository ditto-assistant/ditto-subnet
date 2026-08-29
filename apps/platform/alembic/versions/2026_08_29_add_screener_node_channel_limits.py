"""Add per-node screener channel limits and job ownership.

Revision ID: a7d1f3c5e902
Revises: c7d91f4a2e60
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7d1f3c5e902"
down_revision: str | Sequence[str] | None = "c7d91f4a2e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_node_channel_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            name="screener_node_channel_settings_environment_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="screener_node_channel_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_node_channel_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_node_channel_settings_actor_check",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["screener_nodes.node_id"],
            name="screener_node_channel_settings_node_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "node_id",
            "parent_revision",
            name="screener_node_channel_settings_node_parent_key",
        ),
    )
    op.create_index(
        "screener_node_channel_settings_node_revision_idx",
        "screener_node_channel_settings_revisions",
        ["node_id", "revision"],
        unique=True,
    )

    op.add_column("submission_image_builds", sa.Column("node_id", sa.Text()))
    op.add_column("submission_image_builds", sa.Column("runtime_node_id", sa.Text()))
    op.create_foreign_key(
        "submission_image_builds_node_id_fkey",
        "submission_image_builds",
        "screener_nodes",
        ["node_id"],
        ["node_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "submission_image_builds_node_status_idx",
        "submission_image_builds",
        ["node_id", "status"],
    )
    op.create_index(
        "submission_image_builds_runtime_node_status_idx",
        "submission_image_builds",
        ["runtime_node_id", "runtime_status"],
    )
    op.create_foreign_key(
        "submission_image_builds_runtime_node_id_fkey",
        "submission_image_builds",
        "screener_nodes",
        ["runtime_node_id"],
        ["node_id"],
        ondelete="SET NULL",
    )
    op.add_column("submission_source_reviews", sa.Column("node_id", sa.Text()))
    op.create_foreign_key(
        "submission_source_reviews_node_id_fkey",
        "submission_source_reviews",
        "screener_nodes",
        ["node_id"],
        ["node_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "submission_source_reviews_node_status_idx",
        "submission_source_reviews",
        ["node_id", "status"],
    )

    for table in (
        "trusted_image_builds",
        "submission_image_builds",
        "submission_source_reviews",
    ):
        op.drop_constraint(f"{table}_provider_check", table, type_="check")
        op.create_check_constraint(
            f"{table}_provider_check",
            table,
            "provider IS NULL OR provider IN ('targon', 'gcp', 'hetzner')",
        )


def downgrade() -> None:
    for table in (
        "trusted_image_builds",
        "submission_image_builds",
        "submission_source_reviews",
    ):
        op.drop_constraint(f"{table}_provider_check", table, type_="check")
        op.create_check_constraint(
            f"{table}_provider_check",
            table,
            "provider IS NULL OR provider IN ('targon', 'gcp')",
        )
    op.drop_index(
        "submission_source_reviews_node_status_idx",
        table_name="submission_source_reviews",
    )
    op.drop_constraint(
        "submission_source_reviews_node_id_fkey",
        "submission_source_reviews",
        type_="foreignkey",
    )
    op.drop_column("submission_source_reviews", "node_id")
    op.drop_index(
        "submission_image_builds_runtime_node_status_idx",
        table_name="submission_image_builds",
    )
    op.drop_index(
        "submission_image_builds_node_status_idx",
        table_name="submission_image_builds",
    )
    op.drop_constraint(
        "submission_image_builds_runtime_node_id_fkey",
        "submission_image_builds",
        type_="foreignkey",
    )
    op.drop_constraint(
        "submission_image_builds_node_id_fkey",
        "submission_image_builds",
        type_="foreignkey",
    )
    op.drop_column("submission_image_builds", "runtime_node_id")
    op.drop_column("submission_image_builds", "node_id")
    op.drop_index(
        "screener_node_channel_settings_node_revision_idx",
        table_name="screener_node_channel_settings_revisions",
    )
    op.drop_table("screener_node_channel_settings_revisions")

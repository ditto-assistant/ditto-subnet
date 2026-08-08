"""add append-only emission burn settings

Revision ID: e2b7c4a1d590
Revises: c8a2f491e7d3
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2b7c4a1d590"
down_revision: str | Sequence[str] | None = "c8a2f491e7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "burn_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("scope = '*'", name="burn_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="burn_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="burn_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="burn_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="burn_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="burn_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "burn_settings_scope_revision_idx",
        "burn_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "burn_settings_scope_revision_idx",
        table_name="burn_settings_revisions",
    )
    op.drop_table("burn_settings_revisions")

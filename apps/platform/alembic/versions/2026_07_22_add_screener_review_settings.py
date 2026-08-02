"""add append-only screener review settings

Revision ID: b4c9e2f71a08
Revises: f1a2c3d4e5b6
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4c9e2f71a08"
down_revision: str | Sequence[str] | None = "f1a2c3d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "screener_review_settings_revisions",
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
        sa.CheckConstraint(
            "scope = '*' OR length(scope) BETWEEN 1 AND 63",
            name="screener_review_settings_scope_check",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="screener_review_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="screener_review_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_review_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="screener_review_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "screener_review_settings_scope_revision_idx",
        "screener_review_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "screener_review_settings_scope_revision_idx",
        table_name="screener_review_settings_revisions",
    )
    op.drop_table("screener_review_settings_revisions")

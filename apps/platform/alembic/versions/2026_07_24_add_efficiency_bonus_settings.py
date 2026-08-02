"""add append-only hot-swappable efficiency bonus settings

Revision ID: c3f7a1d9b6e2
Revises: e7a2c4b91d63
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3f7a1d9b6e2"
down_revision: str | Sequence[str] | None = "e7a2c4b91d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "efficiency_bonus_settings_revisions",
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
            name="efficiency_bonus_settings_scope_check",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="efficiency_bonus_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="efficiency_bonus_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="efficiency_bonus_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="efficiency_bonus_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="efficiency_bonus_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "efficiency_bonus_settings_scope_revision_idx",
        "efficiency_bonus_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "efficiency_bonus_settings_scope_revision_idx",
        table_name="efficiency_bonus_settings_revisions",
    )
    op.drop_table("efficiency_bonus_settings_revisions")

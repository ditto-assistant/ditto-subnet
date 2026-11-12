"""add audited unreleased-copy settings revisions

Revision ID: 7a3e5c1d9f02
Revises: 3f6a8d2c9e14
Create Date: 2026-11-01

Append-only mode (off | shadow | enforce) and threshold for the unreleased-copy
signal, in the same optimistic-revision shape as the copy-court settings. No
row means shadow at the default threshold, which is what production ran
before this table existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7a3e5c1d9f02"
down_revision: str | Sequence[str] | None = "3f6a8d2c9e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "unreleased_copy_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('off', 'shadow', 'enforce')",
            name="unreleased_copy_settings_mode_check",
        ),
        sa.CheckConstraint(
            "threshold > 0 AND threshold <= 100000",
            name="unreleased_copy_settings_threshold_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="unreleased_copy_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="unreleased_copy_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision", name="unreleased_copy_settings_parent_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("unreleased_copy_settings_revisions")

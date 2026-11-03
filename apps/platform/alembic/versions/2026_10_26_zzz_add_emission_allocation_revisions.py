"""add append-only typed-weights emission allocation revisions

Revision ID: 7c3e9a1b5d42
Revises: 9e4b2d7c1a63
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c3e9a1b5d42"
down_revision: str | Sequence[str] | None = "9e4b2d7c1a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "emission_allocation_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("allocation", json_type, nullable=False),
        sa.Column("served", json_type, nullable=False),
        sa.Column("burn_settings_revision", sa.Integer(), nullable=False),
        sa.Column("track_share_settings_revision", sa.Integer(), nullable=False),
        sa.Column("treasury_settings_revision", sa.Integer(), nullable=False),
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
            "parent_revision >= 0",
            name="emission_allocation_parent_revision_check",
        ),
        sa.CheckConstraint(
            "burn_settings_revision >= 0 AND track_share_settings_revision >= 0 "
            "AND treasury_settings_revision >= 0",
            name="emission_allocation_settings_revision_check",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64", name="emission_allocation_checksum_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="emission_allocation_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="emission_allocation_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint("parent_revision", name="emission_allocation_parent_key"),
    )


def downgrade() -> None:
    op.drop_table("emission_allocation_revisions")

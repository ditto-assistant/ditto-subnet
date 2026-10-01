"""add operator-controlled scoring lease settings

One new append-only settings table, ``scoring_lease_settings_revisions``,
mirroring ``inference_concurrency_settings_revisions`` (ditto-subnet #1156).

There is deliberately **no backfill row**. The shipped default in
``ditto.api_models.scoring_lease_settings`` is the pre-migration 180-minute
constant, so an empty table is the exact prior behaviour and the operator
history contains only operator decisions.

Revision ID: 9c41d7e2b6a3
Revises: 6a7a2a03a65f
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9c41d7e2b6a3"
down_revision: str | Sequence[str] | None = "6a7a2a03a65f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "scoring_lease_settings_revisions",
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
        sa.CheckConstraint("scope = '*'", name="scoring_lease_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="scoring_lease_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="scoring_lease_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="scoring_lease_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="scoring_lease_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="scoring_lease_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "scoring_lease_settings_scope_revision_idx",
        "scoring_lease_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "scoring_lease_settings_scope_revision_idx",
        table_name="scoring_lease_settings_revisions",
    )
    op.drop_table("scoring_lease_settings_revisions")

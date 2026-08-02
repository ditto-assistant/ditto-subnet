"""add append-only hot-swappable validator slot settings

Revision ID: f1c8d34a7b95
Revises: b7e6d5c4a3f2
Create Date: 2026-07-25

Note on heads: this was authored against ``b7e6d5c4a3f2``, the single head at the
time. A sibling revision was being authored concurrently against the SAME parent,
so if ``alembic heads`` reports two heads after both land, the two migrations are
INDEPENDENT (disjoint tables, no shared columns or constraints) and simply need an
``alembic merge`` revision -- they can be applied in either order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1c8d34a7b95"
down_revision: str | Sequence[str] | None = "dbc8ebf20a23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "validator_slot_settings_revisions",
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
            name="validator_slot_settings_scope_check",
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="validator_slot_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="validator_slot_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="validator_slot_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_slot_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="validator_slot_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "validator_slot_settings_scope_revision_idx",
        "validator_slot_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "validator_slot_settings_scope_revision_idx",
        table_name="validator_slot_settings_revisions",
    )
    op.drop_table("validator_slot_settings_revisions")

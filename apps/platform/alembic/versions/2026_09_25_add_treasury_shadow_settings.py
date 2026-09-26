"""Add append-only shadow treasury allocation policy.

Revision ID: b7e2a4d961c0
Revises: 9e4c7a1b6d20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e2a4d961c0"
down_revision: str | Sequence[str] | None = "9e4c7a1b6d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "treasury_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column(
            "settings",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "parent_revision >= 0", name="treasury_settings_parent_check"
        ),
        sa.CheckConstraint(
            "length(checksum) = 64", name="treasury_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="treasury_settings_reason_check"
        ),
        sa.UniqueConstraint("parent_revision", name="treasury_settings_parent_key"),
    )
    op.execute(
        """CREATE FUNCTION reject_treasury_settings_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'treasury policy history is append only';
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER treasury_settings_immutable
        BEFORE UPDATE OR DELETE ON treasury_settings_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_treasury_settings_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER treasury_settings_immutable ON treasury_settings_revisions"
    )
    op.execute("DROP FUNCTION reject_treasury_settings_mutation()")
    op.drop_table("treasury_settings_revisions")

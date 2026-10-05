"""Append-only public proof and guarded Gamma producer control; no seeded state."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c1a72e9035bf"
down_revision = "b391f0e8c2d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "treasury_runtime_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
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
            "parent_revision >= 0", name="treasury_runtime_parent_check"
        ),
        sa.CheckConstraint(
            "length(checksum) = 64", name="treasury_runtime_checksum_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="treasury_runtime_reason_check",
        ),
        sa.UniqueConstraint("parent_revision", name="treasury_runtime_parent_key"),
    )
    op.execute(
        "CREATE TRIGGER treasury_runtime_immutable BEFORE UPDATE OR DELETE "
        "ON treasury_runtime_revisions FOR EACH ROW "
        "EXECUTE FUNCTION reject_treasury_settings_mutation()"
    )


def downgrade():
    op.execute("LOCK TABLE treasury_runtime_revisions IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM treasury_runtime_revisions)")
    ):
        raise RuntimeError("Retained Gamma runtime history prevents downgrade")
    op.drop_table("treasury_runtime_revisions")

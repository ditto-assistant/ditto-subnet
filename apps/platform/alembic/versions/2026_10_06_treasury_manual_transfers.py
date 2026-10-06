"""Durable manual control outbox and public-coordinate return inbox."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f4d95a6c827b"
down_revision = "b4c9f7e2a810"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "treasury_manual_bridge_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "treasury_manual_transfers",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("report", postgresql.JSONB()),
        sa.Column("receipt", postgresql.JSONB()),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('queued','dispatched','pending','audit_pending',"
            "'published','failed','refused')",
            name="treasury_manual_status",
        ),
        sa.CheckConstraint(
            "length(digest)=64 AND length(actor) BETWEEN 1 AND 254",
            name="treasury_manual_identity",
        ),
    )
    op.create_index(
        "treasury_manual_unfinished",
        "treasury_manual_transfers",
        ["status", "created_at"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM treasury_manual_transfers)")
    ):
        raise RuntimeError("Manual transfer audit history prevents downgrade")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM treasury_manual_bridge_state)")
    ):
        raise RuntimeError("Manual bridge state prevents downgrade")
    op.drop_table("treasury_manual_transfers")
    op.drop_table("treasury_manual_bridge_state")

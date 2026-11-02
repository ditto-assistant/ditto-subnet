"""Durable weight submission provenance, preserving existing task rows."""

import sqlalchemy as sa
from alembic import op

revision = "ditto_receipts_v1"
down_revision = "daab35a40458"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("ditto_weight_receipts"):
        # A version-marker rollback deliberately preserves all evidence. The
        # next upgrade must recognize that exact table, never overwrite it.
        expected = {
            "request_id",
            "task_id",
            "identity_name",
            "netuid",
            "request_digest",
            "body",
            "attempt",
            "acknowledged",
            "created_at",
            "acknowledged_at",
        }
        if {
            column["name"] for column in inspector.get_columns("ditto_weight_receipts")
        } != expected:
            raise RuntimeError("retained receipt table has an unexpected schema")
        return
    op.create_table(
        "ditto_weight_receipts",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("weight_tasks.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("identity_name", sa.String(), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("body", sa.JSON(), nullable=True),
        sa.Column("attempt", sa.JSON(), nullable=True),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ditto_weight_receipts_identity_name",
        "ditto_weight_receipts",
        ["identity_name"],
    )


def downgrade() -> None:
    # Version-marker rollback only. Existing Pylon knows the parent revision and
    # ignores the additive table; receipts and their task references survive.
    # Stop this image before rollback and retain the database for re-upgrade.
    pass

"""Fence receipt-backed partial distributions by cumulative bucket entitlement."""

import sqlalchemy as sa

from alembic import op

revision = "e2b5f0a9467c"
down_revision = "c1a72e9035bf"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index(
        "treasury_verified_distribution_once", table_name="treasury_verified_receipts"
    )
    op.create_index(
        "treasury_verified_distribution_source",
        "treasury_verified_receipts",
        ["epoch_index", "source_block", "bucket_id"],
        postgresql_where=sa.text("stage = 'service_distribution'"),
    )


def downgrade():
    op.execute("LOCK TABLE treasury_verified_receipts IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM treasury_verified_receipts "
            "WHERE stage = 'service_distribution')"
        )
    ):
        raise RuntimeError("Retained distributions prevent single-leg downgrade")
    op.drop_index(
        "treasury_verified_distribution_source", table_name="treasury_verified_receipts"
    )
    op.create_index(
        "treasury_verified_distribution_once",
        "treasury_verified_receipts",
        ["epoch_index", "source_block", "bucket_id"],
        unique=True,
        postgresql_where=sa.text("stage = 'service_distribution'"),
    )

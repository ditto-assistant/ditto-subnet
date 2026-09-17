"""Immutable private benchmark artifacts; no live routing change.

Revision ID: 61ef904ca8d2
Revises: f2c8d41a6b90
"""

import sqlalchemy as sa

from alembic import op

revision = "61ef904ca8d2"
down_revision = "f2c8d41a6b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "private_benchmark_datasets",
        sa.Column("dataset_id", sa.Uuid(), primary_key=True),
        sa.Column("identity_sha256", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("run_size", sa.Text(), nullable=False),
        sa.Column("transform_profile_sha256", sa.Text(), nullable=False),
        sa.Column("validation_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("validation_receipt_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("base_sha256", sa.Text(), nullable=False),
        sa.Column("dataset_sha256", sa.Text(), nullable=False),
        sa.Column("base_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("dataset_bytes", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("identity_sha256"),
        sa.CheckConstraint("bench_version = 13 AND seed >= 0", name="version_seed"),
        sa.CheckConstraint("run_size IN ('small', 'medium', 'full')", name="run_size"),
        sa.CheckConstraint("length(scope) BETWEEN 1 AND 256", name="scope"),
        sa.CheckConstraint(
            "octet_length(base_bytes) BETWEEN 1 AND 33554432 AND "
            "octet_length(dataset_bytes) BETWEEN 1 AND 33554432",
            name="size",
        ),
        sa.CheckConstraint(
            "base_sha256 = encode(sha256(base_bytes), 'hex') AND "
            "dataset_sha256 = encode(sha256(dataset_bytes), 'hex') AND "
            "validation_receipt_sha256 = "
            "encode(sha256(validation_receipt_bytes), 'hex') AND "
            "octet_length(validation_receipt_bytes) BETWEEN 1 AND 1048576",
            name="content_hashes",
        ),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' AND "
            "transform_profile_sha256 ~ '^[0-9a-f]{64}$' AND "
            "validation_receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="digest_format",
        ),
    )
    op.execute("""
        CREATE FUNCTION reject_private_benchmark_dataset_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'private benchmark dataset is immutable';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER private_benchmark_dataset_immutable
        BEFORE UPDATE OR DELETE ON private_benchmark_datasets
        FOR EACH ROW EXECUTE FUNCTION reject_private_benchmark_dataset_mutation();
    """)


def downgrade() -> None:
    # Never discard private artifacts that may back signed reports on rollback.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM private_benchmark_datasets) THEN
            RAISE EXCEPTION 'cannot downgrade with retained private datasets';
          END IF;
        END $$;
    """)
    op.drop_table("private_benchmark_datasets")
    op.execute("DROP FUNCTION reject_private_benchmark_dataset_mutation()")

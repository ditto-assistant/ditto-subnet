"""Durable private dataset preparation with immutable entropy and claim fencing.

Revision ID: 82fb617ce934
Revises: 61ef904ca8d2
"""

import sqlalchemy as sa

from alembic import op

revision = "82fb617ce934"
down_revision = "61ef904ca8d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "private_benchmark_preparations",
        sa.Column("preparation_id", sa.Uuid(), primary_key=True),
        sa.Column("identity_sha256", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("run_size", sa.Text(), nullable=False),
        sa.Column("transform_profile_sha256", sa.Text(), nullable=False),
        sa.Column("surface_salt", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("claim_until", sa.DateTime(timezone=True)),
        sa.Column("dataset_id", sa.Uuid()),
        sa.Column("failure_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("identity_sha256"),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["private_benchmark_datasets.dataset_id"]
        ),
        sa.CheckConstraint("bench_version = 13 AND seed >= 0", name="version_seed"),
        sa.CheckConstraint("run_size IN ('small', 'medium', 'full')", name="run_size"),
        sa.CheckConstraint("length(scope) BETWEEN 1 AND 256", name="scope"),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' AND "
            "transform_profile_sha256 ~ '^[0-9a-f]{64}$'",
            name="digest_format",
        ),
        sa.CheckConstraint(
            "octet_length(surface_salt) = 8 AND "
            "surface_salt <> decode('0000000000000000', 'hex')",
            name="salt",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'ready', 'failed') AND "
            "attempts BETWEEN 0 AND 3",
            name="state_attempts",
        ),
        sa.CheckConstraint(
            "(state = 'running') = (claim_until IS NOT NULL) AND "
            "(state <> 'running' OR claim_token IS NOT NULL) AND "
            "(state = 'ready') = (dataset_id IS NOT NULL)",
            name="state_binding",
        ),
    )
    op.create_index(
        "ix_private_benchmark_preparation_claim",
        "private_benchmark_preparations",
        ["transform_profile_sha256", "state", "created_at"],
    )
    op.execute("""
        CREATE FUNCTION protect_private_benchmark_preparation_identity()
        RETURNS trigger AS $$ BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'private preparation history is retained';
          END IF;
          IF ROW(OLD.preparation_id, OLD.identity_sha256, OLD.scope,
                 OLD.bench_version, OLD.seed, OLD.run_size,
                 OLD.transform_profile_sha256, OLD.surface_salt, OLD.created_at)
             IS DISTINCT FROM
             ROW(NEW.preparation_id, NEW.identity_sha256, NEW.scope,
                 NEW.bench_version, NEW.seed, NEW.run_size,
                 NEW.transform_profile_sha256, NEW.surface_salt, NEW.created_at)
             OR OLD.state = 'ready' THEN
            RAISE EXCEPTION 'private preparation identity or ready result is immutable';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER private_benchmark_preparation_identity
        BEFORE UPDATE OR DELETE ON private_benchmark_preparations
        FOR EACH ROW EXECUTE FUNCTION protect_private_benchmark_preparation_identity();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM private_benchmark_preparations) THEN
            RAISE EXCEPTION 'cannot downgrade retained private preparations';
          END IF;
        END $$;
    """)
    op.drop_table("private_benchmark_preparations")
    op.execute("DROP FUNCTION protect_private_benchmark_preparation_identity()")

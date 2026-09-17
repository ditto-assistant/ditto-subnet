"""Add isolated benchmark canary receipts and a non-scoring ticket purpose.

Revision ID: f2c8d41a6b90
Revises: 9e4b7c2d1a63
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f2c8d41a6b90"
down_revision = "9e4b7c2d1a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_canaries",
        sa.Column("canary_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "agent_id", sa.Uuid(), sa.ForeignKey("agents.agent_id"), nullable=False
        ),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("dataset_sha256", sa.Text(), nullable=False),
        sa.Column("run_size", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql")),
        sa.Column("signature", sa.Text()),
        sa.Column("failure_detail", sa.Text()),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "deadline",
            name="benchmark_canaries_lease_key",
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'completed', 'failed', 'cancelled')",
            name="benchmark_canaries_status",
        ),
        sa.CheckConstraint(
            "bench_version > 0 AND seed >= 0", name="benchmark_canaries_version_seed"
        ),
    )
    # Metadata-only replacement; no ticket backfill or hot-table column change.
    op.drop_constraint(
        "validator_tickets_purpose_valid", "validator_tickets", type_="check"
    )
    op.create_check_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        "purpose IN ('legacy_unclassified', 'canonical_quorum', "
        "'continual_retest', 'benchmark_canary')",
    )
    # Defense in depth against old/misrouted writers: a diagnostic capability
    # can never authorize a canonical or confirmation score row.
    op.execute("""
        CREATE FUNCTION reject_benchmark_canary_score() RETURNS trigger AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM validator_tickets t
              WHERE t.agent_id = NEW.agent_id
                AND t.bench_version = NEW.bench_version
                AND t.validator_hotkey = NEW.validator_hotkey
                AND t.purpose = 'benchmark_canary') THEN
            RAISE EXCEPTION 'benchmark canary cannot write authoritative scores';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER scores_reject_benchmark_canary
          BEFORE INSERT OR UPDATE ON scores
          FOR EACH ROW EXECUTE FUNCTION reject_benchmark_canary_score();
    """)
    op.execute("""
        CREATE TRIGGER confirmation_scores_reject_benchmark_canary
          BEFORE INSERT OR UPDATE ON confirmation_scores
          FOR EACH ROW EXECUTE FUNCTION reject_benchmark_canary_score();
    """)


def downgrade() -> None:
    # Refuse rollback while diagnostic capabilities still exist. Never relabel
    # one as canonical just to make a downgrade pass its constraint.
    op.execute(
        "DROP TRIGGER confirmation_scores_reject_benchmark_canary "
        "ON confirmation_scores"
    )
    op.execute("DROP TRIGGER scores_reject_benchmark_canary ON scores")
    op.execute("DROP FUNCTION reject_benchmark_canary_score()")
    op.drop_constraint(
        "validator_tickets_purpose_valid", "validator_tickets", type_="check"
    )
    op.create_check_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        "purpose IN ('legacy_unclassified', 'canonical_quorum', 'continual_retest')",
    )
    op.drop_table("benchmark_canaries")

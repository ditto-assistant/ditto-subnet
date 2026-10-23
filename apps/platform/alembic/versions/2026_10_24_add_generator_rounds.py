"""add chain-epoch generator rounds and the generator_round ticket purpose

Revision ID: 8e2f4c6a1d93
Revises: 5c1e9a7d2b40
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8e2f4c6a1d93"
down_revision: str | Sequence[str] | None = "5c1e9a7d2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSES = (
    "'legacy_unclassified', 'canonical_quorum', 'continual_retest', 'benchmark_canary'"
)


def _tz() -> sa.types.TypeEngine:
    return sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    # generator_submissions holds no production rows yet (the lane ships
    # mode=off), so plain column adds take no meaningful lock.
    op.add_column("generator_submissions", sa.Column("qualified_at", _tz()))
    op.add_column(
        "generator_submissions",
        sa.Column(
            "public_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("generator_submissions", sa.Column("round_score", sa.Float()))
    op.add_column(
        "generator_submissions", sa.Column("round_scored_index", sa.BigInteger())
    )
    op.create_index(
        "generator_submissions_public_default_key",
        "generator_submissions",
        [sa.text("(public_default)")],
        unique=True,
        postgresql_where=sa.text("public_default AND status = 'active'"),
    )
    op.drop_constraint("generator_runs_status_check", "generator_runs", type_="check")
    op.create_check_constraint(
        "generator_runs_status_check",
        "generator_runs",
        "status IN ('leased', 'scored', 'qualified', 'invalid', "
        "'insufficient_panel', 'copy', 'abandoned')",
    )

    op.create_table(
        "generator_rounds",
        sa.Column("round_index", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("epochs_per_round", sa.Integer(), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("commitment", sa.Text(), nullable=False),
        sa.Column("committed_at", _tz(), nullable=False),
        sa.Column("committed_epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("commitment_publication", sa.Text(), nullable=False),
        sa.Column("commitment_published_at", _tz()),
        sa.Column("commitment_error", sa.Text()),
        sa.Column("open_epoch_index", sa.BigInteger()),
        sa.Column("open_block", sa.BigInteger()),
        sa.Column("open_block_hash", sa.Text()),
        sa.Column("opened_at", _tz()),
        sa.Column("seed", sa.Text()),
        sa.Column("settings_revision", sa.Integer()),
        sa.Column("cases_per_column", sa.Integer()),
        sa.Column("columns", postgresql.JSONB()),
        sa.Column("panel", postgresql.JSONB()),
        sa.Column("lease_id", sa.UUID()),
        sa.Column("lease_expires_at", _tz()),
        sa.Column("lease_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation", postgresql.JSONB()),
        sa.Column("dataset_sha256", sa.Text()),
        sa.Column("dataset_bytes", sa.LargeBinary()),
        sa.Column("column_cases", postgresql.JSONB()),
        sa.Column("closed_at", _tz()),
        sa.Column("score", postgresql.JSONB()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("scored_at", _tz()),
        sa.Column("void_reason", sa.Text()),
        sa.Column("revealed_at", _tz()),
        sa.CheckConstraint(
            "status IN ('committed', 'generating', 'running', 'scoring', "
            "'scored', 'void')",
            name="generator_rounds_status_check",
        ),
        sa.CheckConstraint(
            "commitment ~ '^[0-9a-f]{64}$' AND octet_length(secret) = 32",
            name="generator_rounds_commitment_check",
        ),
        sa.CheckConstraint(
            "commitment_publication IN ('pending', 'published', 'failed', 'disabled')",
            name="generator_rounds_publication_check",
        ),
        sa.CheckConstraint(
            "(status <> 'committed' OR seed IS NULL) AND "
            "(seed IS NOT NULL OR status IN ('committed', 'void'))",
            name="generator_rounds_seed_check",
        ),
        sa.CheckConstraint(
            "(dataset_sha256 IS NULL) = (dataset_bytes IS NULL) AND "
            "(dataset_sha256 IS NULL OR "
            "dataset_sha256 = encode(sha256(dataset_bytes), 'hex'))",
            name="generator_rounds_dataset_check",
        ),
        sa.CheckConstraint(
            "(lease_id IS NULL) = (lease_expires_at IS NULL)",
            name="generator_rounds_lease_check",
        ),
        sa.CheckConstraint(
            "(status = 'void') = (void_reason IS NOT NULL)",
            name="generator_rounds_void_check",
        ),
    )
    op.create_index(
        "generator_rounds_dataset_idx",
        "generator_rounds",
        ["dataset_sha256"],
        postgresql_where=sa.text("dataset_sha256 IS NOT NULL"),
    )
    op.create_table(
        "generator_round_results",
        sa.Column("round_index", sa.BigInteger(), primary_key=True),
        sa.Column("agent_id", sa.UUID(), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("ticket_deadline", _tz(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column("answered", sa.Integer(), nullable=False),
        sa.Column("faulted", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("recorded_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["round_index"], ["generator_rounds.round_index"], ondelete="CASCADE"
        ),
    )
    op.create_table(
        "generator_round_attempts",
        sa.Column("round_index", sa.BigInteger(), primary_key=True),
        sa.Column("agent_id", sa.UUID(), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("ticket_deadline", _tz(), primary_key=True),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("issued_at", _tz(), nullable=False),
        sa.Column("finished_at", _tz()),
        sa.ForeignKeyConstraint(
            ["round_index"], ["generator_rounds.round_index"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('leased', 'completed', 'failed')",
            name="generator_round_attempts_status_check",
        ),
    )
    op.create_index(
        "generator_round_attempts_round_idx",
        "generator_round_attempts",
        ["round_index", "agent_id"],
    )
    op.create_table(
        "generator_round_case_verdicts",
        sa.Column("round_index", sa.BigInteger(), primary_key=True),
        sa.Column("column_id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("submission_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("panel_size", sa.Integer(), nullable=False),
        sa.Column("solvers", sa.Integer(), nullable=False),
        sa.Column("solver_owners", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=False),
        sa.Column("champion_failed", sa.Boolean(), nullable=False),
        sa.Column("credit", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["round_index"], ["generator_rounds.round_index"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('discriminating', 'trivial', 'unsolved', 'duplicate', "
            "'insufficient_panel')",
            name="generator_round_case_verdicts_status_check",
        ),
    )
    op.create_index(
        "generator_round_case_verdicts_fingerprint_idx",
        "generator_round_case_verdicts",
        ["fingerprint"],
    )
    op.create_index(
        "generator_round_case_verdicts_submission_idx",
        "generator_round_case_verdicts",
        ["submission_id", "round_index"],
    )

    # Metadata-only replacement, exactly as the canary migration swapped the
    # same constraint; no ticket backfill or hot-table column change.
    op.drop_constraint(
        "validator_tickets_purpose_valid", "validator_tickets", type_="check"
    )
    op.create_check_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        f"purpose IN ({_PURPOSES}, 'generator_round')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE validator_tickets SET purpose = 'continual_retest' "
        "WHERE purpose = 'generator_round'"
    )
    op.drop_constraint(
        "validator_tickets_purpose_valid", "validator_tickets", type_="check"
    )
    op.create_check_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        f"purpose IN ({_PURPOSES})",
    )
    op.drop_table("generator_round_case_verdicts")
    op.drop_table("generator_round_attempts")
    op.drop_table("generator_round_results")
    op.drop_table("generator_rounds")
    op.execute(
        "UPDATE generator_runs SET status = 'scored', score = 0 "
        "WHERE status = 'qualified'"
    )
    op.drop_constraint("generator_runs_status_check", "generator_runs", type_="check")
    op.create_check_constraint(
        "generator_runs_status_check",
        "generator_runs",
        "status IN ('leased', 'scored', 'invalid', 'insufficient_panel', "
        "'copy', 'abandoned')",
    )
    op.drop_index(
        "generator_submissions_public_default_key", table_name="generator_submissions"
    )
    op.drop_column("generator_submissions", "round_scored_index")
    op.drop_column("generator_submissions", "round_score")
    op.drop_column("generator_submissions", "public_default")
    op.drop_column("generator_submissions", "qualified_at")

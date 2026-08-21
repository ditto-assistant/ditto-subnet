"""add finalized shadow coding run issuance

Revision ID: b5c8e1f37a42
Revises: a4b7d2e90f31
Create Date: 2026-08-21

An issuance is the append-only bridge from one pre-revelation assignment to
one selected shared run. It stores no private task bytes and remains
permanently weight-ineligible.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5c8e1f37a42"
down_revision: str | Sequence[str] | None = "a4b7d2e90f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "coding_selection_assignments_run_authority_key",
        "coding_selection_assignments",
        [
            "assignment_row_id",
            "assignment_sha256",
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "coding_contract_version",
            "coding_run_id",
            "corpus_release_id",
            "selection_block_number",
            "task_count",
        ],
    )
    op.create_unique_constraint(
        "coding_shadow_runs_issuance_authority_key",
        "coding_shadow_runs",
        [
            "run_row_id",
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "coding_contract_version",
            "coding_run_id",
            "corpus_release_id",
            "selection_block_number",
            "selection_block_hash",
            "task_count",
        ],
    )
    op.create_table(
        "coding_shadow_run_issuances",
        sa.Column("assignment_row_id", sa.UUID(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("assignment_sha256", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("coding_run_id", sa.Text(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("selection_block_number", sa.BigInteger(), nullable=False),
        sa.Column("selection_block_hash", sa.Text(), nullable=False),
        sa.Column("selection_candidate_probe", sa.Integer(), nullable=False),
        sa.Column("selection_catalog_index", sa.Integer(), nullable=False),
        sa.Column("selection_proof_sha256", sa.Text(), nullable=False),
        sa.Column(
            "selection_block_timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "assignment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND selection_proof_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_shadow_run_issuances_hashes_check",
        ),
        sa.CheckConstraint(
            "selection_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_shadow_run_issuances_block_hash_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND selection_block_number > 0 AND task_count = 1 "
            "AND selection_candidate_probe BETWEEN 0 AND 999999 "
            "AND selection_catalog_index BETWEEN 0 AND 999999 "
            "AND weight_eligible = false "
            "AND issued_at >= selection_block_timestamp - interval '5 seconds'",
            name="coding_shadow_run_issuances_contract_check",
        ),
        sa.CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]'",
            name="coding_shadow_run_issuances_identifiers_check",
        ),
        sa.ForeignKeyConstraint(
            [
                "assignment_row_id",
                "assignment_sha256",
                "agent_id",
                "artifact_sha256",
                "screened_image_sha256",
                "bench_version",
                "coding_contract_version",
                "coding_run_id",
                "corpus_release_id",
                "selection_block_number",
                "task_count",
            ],
            [
                "coding_selection_assignments.assignment_row_id",
                "coding_selection_assignments.assignment_sha256",
                "coding_selection_assignments.agent_id",
                "coding_selection_assignments.artifact_sha256",
                "coding_selection_assignments.screened_image_sha256",
                "coding_selection_assignments.bench_version",
                "coding_selection_assignments.coding_contract_version",
                "coding_selection_assignments.coding_run_id",
                "coding_selection_assignments.corpus_release_id",
                "coding_selection_assignments.selection_block_number",
                "coding_selection_assignments.task_count",
            ],
            name="coding_shadow_run_issuances_assignment_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "run_row_id",
                "agent_id",
                "artifact_sha256",
                "screened_image_sha256",
                "bench_version",
                "coding_contract_version",
                "coding_run_id",
                "corpus_release_id",
                "selection_block_number",
                "selection_block_hash",
                "task_count",
            ],
            [
                "coding_shadow_runs.run_row_id",
                "coding_shadow_runs.agent_id",
                "coding_shadow_runs.artifact_sha256",
                "coding_shadow_runs.screened_image_sha256",
                "coding_shadow_runs.bench_version",
                "coding_shadow_runs.coding_contract_version",
                "coding_shadow_runs.coding_run_id",
                "coding_shadow_runs.corpus_release_id",
                "coding_shadow_runs.selection_block_number",
                "coding_shadow_runs.selection_block_hash",
                "coding_shadow_runs.task_count",
            ],
            name="coding_shadow_run_issuances_run_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "assignment_row_id",
            name="coding_shadow_run_issuances_pkey",
        ),
        sa.UniqueConstraint(
            "run_row_id",
            name="coding_shadow_run_issuances_run_key",
        ),
    )
    op.create_index(
        "coding_shadow_run_issuances_agent_issued_idx",
        "coding_shadow_run_issuances",
        ["agent_id", "issued_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE TRIGGER coding_shadow_run_issuances_append_only
        BEFORE UPDATE OR DELETE ON coding_shadow_run_issuances
        FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS coding_shadow_run_issuances_append_only "
        "ON coding_shadow_run_issuances"
    )
    op.drop_index(
        "coding_shadow_run_issuances_agent_issued_idx",
        table_name="coding_shadow_run_issuances",
    )
    op.drop_table("coding_shadow_run_issuances")
    op.drop_constraint(
        "coding_shadow_runs_issuance_authority_key",
        "coding_shadow_runs",
        type_="unique",
    )
    op.drop_constraint(
        "coding_selection_assignments_run_authority_key",
        "coding_selection_assignments",
        type_="unique",
    )

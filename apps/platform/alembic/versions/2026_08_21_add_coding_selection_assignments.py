"""add append-only shadow coding selection assignments

Revision ID: a4b7d2e90f31
Revises: fa63c1d8e4b2
Create Date: 2026-08-21

Assignments commit an exact screened artifact and private catalog to a future
height before selection. They contain no task, repository, memory, or grader
bytes and remain permanently weight-ineligible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a4b7d2e90f31"
down_revision: str | Sequence[str] | None = "fa63c1d8e4b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_selection_assignments",
        sa.Column("assignment_row_id", sa.UUID(), nullable=False),
        sa.Column("assignment_sha256", sa.Text(), nullable=False),
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("coding_run_id", sa.Text(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("catalog_commitment_sha256", sa.Text(), nullable=False),
        sa.Column("anchor_block_number", sa.BigInteger(), nullable=False),
        sa.Column("anchor_block_hash", sa.Text(), nullable=False),
        sa.Column("selection_delay_blocks", sa.Integer(), nullable=False),
        sa.Column("selection_block_number", sa.BigInteger(), nullable=False),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("core_qualification_observation_id", sa.UUID(), nullable=False),
        sa.Column("certification_row_id", sa.UUID(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "assignment", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "assignment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_selection_assignments_hashes_check",
        ),
        sa.CheckConstraint(
            "anchor_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_selection_assignments_anchor_hash_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND anchor_block_number > 0 "
            "AND selection_delay_blocks BETWEEN 1 AND 10000 "
            "AND selection_block_number = anchor_block_number + selection_delay_blocks "
            "AND task_count = 1 AND weight_eligible = false",
            name="coding_selection_assignments_contract_check",
        ),
        sa.CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]'",
            name="coding_selection_assignments_identifiers_check",
        ),
        sa.CheckConstraint(
            "assigned_at = created_at",
            name="coding_selection_assignments_time_check",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="coding_selection_assignments_agent_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_row_id", "corpus_release_id"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.corpus_release_id",
            ],
            name="coding_selection_assignments_release_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_row_id", "catalog_commitment_sha256"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.commitment_sha256",
            ],
            name="coding_selection_assignments_commitment_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["core_qualification_observation_id"],
            ["core_qualification_observations.observation_id"],
            name="coding_selection_assignments_core_observation_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["certification_row_id"],
            ["coding_capability_certifications.certification_row_id"],
            name="coding_selection_assignments_certification_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "assignment_row_id", name="coding_selection_assignments_pkey"
        ),
        sa.UniqueConstraint(
            "assignment_sha256",
            name="coding_selection_assignments_assignment_sha256_key",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "coding_contract_version",
            "coding_run_id",
            name="coding_selection_assignments_identity_key",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "coding_contract_version",
            name="coding_selection_assignments_artifact_key",
        ),
    )
    op.create_index(
        "coding_selection_assignments_agent_created_idx",
        "coding_selection_assignments",
        ["agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "coding_selection_assignments_height_idx",
        "coding_selection_assignments",
        ["selection_block_number", "created_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE TRIGGER coding_selection_assignments_append_only
        BEFORE UPDATE OR DELETE ON coding_selection_assignments
        FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS coding_selection_assignments_append_only "
        "ON coding_selection_assignments"
    )
    op.drop_index(
        "coding_selection_assignments_height_idx",
        table_name="coding_selection_assignments",
    )
    op.drop_index(
        "coding_selection_assignments_agent_created_idx",
        table_name="coding_selection_assignments",
    )
    op.drop_table("coding_selection_assignments")

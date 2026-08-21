"""add signed coding catalog commitments and task exposure ledger

Revision ID: fa63c1d8e4b2
Revises: e9f52b8c4d31
Create Date: 2026-08-29

Only commitments and private task-version digests are stored. No repository,
hidden-test, reference-patch, or memory content enters these tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "fa63c1d8e4b2"
down_revision: str | Sequence[str] | None = "e9f52b8c4d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_catalog_releases",
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("catalog_merkle_root", sa.Text(), nullable=False),
        sa.Column("selection_derivation_id", sa.Text(), nullable=False),
        sa.Column("selection_chain_genesis_hash", sa.Text(), nullable=False),
        sa.Column("grader_contract_sha256", sa.Text(), nullable=False),
        sa.Column("inference_grant_sha256", sa.Text(), nullable=False),
        sa.Column("task_version_count", sa.Integer(), nullable=False),
        sa.Column("curator_hotkey", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("commitment_sha256", sa.Text(), nullable=False),
        sa.Column(
            "commitment", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "coding_contract_version = 1 AND weight_eligible = false "
            "AND task_version_count BETWEEN 1 AND 1000000",
            name="coding_catalog_releases_contract_check",
        ),
        sa.CheckConstraint(
            "catalog_merkle_root ~ '^[0-9a-f]{64}$' "
            "AND grader_contract_sha256 ~ '^[0-9a-f]{64}$' "
            "AND inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_catalog_releases_hashes_check",
        ),
        sa.CheckConstraint(
            "selection_chain_genesis_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_catalog_releases_genesis_check",
        ),
        sa.CheckConstraint(
            "curator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$' "
            "AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_catalog_releases_signature_check",
        ),
        sa.CheckConstraint(
            "octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND octet_length(selection_derivation_id) BETWEEN 1 AND 128 "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]' "
            "AND selection_derivation_id !~ '[[:space:][:cntrl:]]'",
            name="coding_catalog_releases_identifiers_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_catalog_releases_audit_check",
        ),
        sa.PrimaryKeyConstraint("release_row_id", name="coding_catalog_releases_pkey"),
        sa.UniqueConstraint(
            "corpus_release_id",
            name="coding_catalog_releases_corpus_release_id_key",
        ),
        sa.UniqueConstraint(
            "commitment_sha256",
            name="coding_catalog_releases_commitment_sha256_key",
        ),
        sa.UniqueConstraint(
            "release_row_id",
            "corpus_release_id",
            name="coding_catalog_releases_row_corpus_key",
        ),
        sa.UniqueConstraint(
            "release_row_id",
            "commitment_sha256",
            name="coding_catalog_releases_row_commitment_key",
        ),
    )
    op.create_index(
        "coding_catalog_releases_created_idx",
        "coding_catalog_releases",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "coding_catalog_retirements",
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("expected_commitment_sha256", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "retired_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_catalog_retirements_commitment_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_catalog_retirements_audit_check",
        ),
        sa.ForeignKeyConstraint(
            ["release_row_id", "expected_commitment_sha256"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.commitment_sha256",
            ],
            name="coding_catalog_retirements_release_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "release_row_id", name="coding_catalog_retirements_pkey"
        ),
    )

    op.create_unique_constraint(
        "coding_shadow_runs_run_corpus_task_count_key",
        "coding_shadow_runs",
        ["run_row_id", "corpus_release_id", "task_count"],
    )

    op.create_table(
        "coding_catalog_exposures",
        sa.Column("exposure_id", sa.UUID(), nullable=False),
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("run_task_count", sa.Integer(), nullable=False),
        sa.Column("manifest_index", sa.Integer(), nullable=False),
        sa.Column("task_version_id", sa.Text(), nullable=False),
        sa.Column("task_commitment_sha256", sa.Text(), nullable=False),
        sa.Column("selection_proof_sha256", sa.Text(), nullable=False),
        sa.Column("catalog_membership_proof_sha256", sa.Text(), nullable=False),
        sa.Column("visible_bundle_sha256", sa.Text(), nullable=False),
        sa.Column("base_tree_sha256", sa.Text(), nullable=False),
        sa.Column("memory_bundle_sha256", sa.Text(), nullable=False),
        sa.Column("environment_image_digest", sa.Text(), nullable=False),
        sa.Column("resource_profile_sha256", sa.Text(), nullable=False),
        sa.Column("grader_bundle_sha256", sa.Text(), nullable=False),
        sa.Column("grader_image_digest", sa.Text(), nullable=False),
        sa.Column("test_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("grader_plan_sha256", sa.Text(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "exposed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_task_count BETWEEN 1 AND 100 "
            "AND manifest_index >= 0 AND manifest_index < run_task_count",
            name="coding_catalog_exposures_index_check",
        ),
        sa.CheckConstraint(
            "octet_length(task_version_id) BETWEEN 1 AND 256 "
            "AND task_version_id !~ '[[:space:][:cntrl:]]'",
            name="coding_catalog_exposures_task_version_check",
        ),
        sa.CheckConstraint(
            "task_commitment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND selection_proof_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_membership_proof_sha256 ~ '^[0-9a-f]{64}$' "
            "AND visible_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND base_tree_sha256 ~ '^[0-9a-f]{64}$' "
            "AND memory_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND resource_profile_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND test_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_plan_sha256 ~ '^[0-9a-f]{64}$' "
            "AND environment_image_digest ~ '^sha256:[0-9a-f]{64}$' "
            "AND grader_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="coding_catalog_exposures_digests_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_catalog_exposures_weight_ineligible",
        ),
        sa.ForeignKeyConstraint(
            ["release_row_id", "corpus_release_id"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.corpus_release_id",
            ],
            name="coding_catalog_exposures_release_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_row_id", "corpus_release_id", "run_task_count"],
            [
                "coding_shadow_runs.run_row_id",
                "coding_shadow_runs.corpus_release_id",
                "coding_shadow_runs.task_count",
            ],
            name="coding_catalog_exposures_run_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("exposure_id", name="coding_catalog_exposures_pkey"),
        sa.UniqueConstraint(
            "release_row_id",
            "task_version_id",
            name="coding_catalog_exposures_task_version_key",
        ),
        sa.UniqueConstraint(
            "run_row_id",
            "manifest_index",
            name="coding_catalog_exposures_run_index_key",
        ),
    )
    op.create_index(
        "coding_catalog_exposures_run_idx",
        "coding_catalog_exposures",
        ["run_row_id", "manifest_index"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION guard_coding_catalog_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'coding catalog ledgers are append-only'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'coding_catalog_append_only_guard';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "coding_catalog_releases",
        "coding_catalog_retirements",
        "coding_catalog_exposures",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only_guard
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()
            """
        )


def downgrade() -> None:
    for table in (
        "coding_catalog_exposures",
        "coding_catalog_retirements",
        "coding_catalog_releases",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS guard_coding_catalog_append_only()")
    op.drop_index(
        "coding_catalog_exposures_run_idx",
        table_name="coding_catalog_exposures",
    )
    op.drop_table("coding_catalog_exposures")
    op.drop_constraint(
        "coding_shadow_runs_run_corpus_task_count_key",
        "coding_shadow_runs",
        type_="unique",
    )
    op.drop_table("coding_catalog_retirements")
    op.drop_index(
        "coding_catalog_releases_created_idx",
        table_name="coding_catalog_releases",
    )
    op.drop_table("coding_catalog_releases")

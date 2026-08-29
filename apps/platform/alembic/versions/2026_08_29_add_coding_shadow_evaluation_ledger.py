"""add separate shadow coding evaluation ledger

Revision ID: e9f52b8c4d31
Revises: d8e41a7c3f20
Create Date: 2026-08-29

The shared run, validator leases, and signed results remain permanently
weight-ineligible and disconnected from the ordinary score table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9f52b8c4d31"
down_revision: str | Sequence[str] | None = "d8e41a7c3f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_shadow_runs",
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("coding_run_id", sa.Text(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("catalog_merkle_root", sa.Text(), nullable=False),
        sa.Column("selection_derivation_id", sa.Text(), nullable=False),
        sa.Column("selection_chain_genesis_hash", sa.Text(), nullable=False),
        sa.Column("selection_block_number", sa.BigInteger(), nullable=False),
        sa.Column("selection_block_hash", sa.Text(), nullable=False),
        sa.Column("inference_grant_sha256", sa.Text(), nullable=False),
        sa.Column("grader_contract_sha256", sa.Text(), nullable=False),
        sa.Column("task_set_id", sa.Text(), nullable=False),
        sa.Column("task_set_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("run_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("core_qualification_observation_id", sa.UUID(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_merkle_root ~ '^[0-9a-f]{64}$' "
            "AND inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_contract_sha256 ~ '^[0-9a-f]{64}$' "
            "AND task_set_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND run_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_shadow_runs_hashes_check",
        ),
        sa.CheckConstraint(
            "selection_chain_genesis_hash ~ '^0x[0-9a-f]{64}$' "
            "AND selection_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_shadow_runs_block_hashes_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND selection_block_number > 0 AND task_count BETWEEN 1 AND 100",
            name="coding_shadow_runs_bounds_check",
        ),
        sa.CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND octet_length(selection_derivation_id) BETWEEN 1 AND 128 "
            "AND octet_length(task_set_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]' "
            "AND selection_derivation_id !~ '[[:space:][:cntrl:]]' "
            "AND task_set_id !~ '[[:space:][:cntrl:]]'",
            name="coding_shadow_runs_identifiers_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_runs_weight_ineligible",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="coding_shadow_runs_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["core_qualification_observation_id"],
            ["core_qualification_observations.observation_id"],
            name="coding_shadow_runs_core_observation_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_row_id", name="coding_shadow_runs_pkey"),
        sa.UniqueConstraint(
            "agent_id",
            "coding_contract_version",
            "coding_run_id",
            name="coding_shadow_runs_identity_key",
        ),
        sa.UniqueConstraint(
            "run_row_id",
            "task_count",
            name="coding_shadow_runs_run_task_count_key",
        ),
    )
    op.create_index(
        "coding_shadow_runs_agent_created_idx",
        "coding_shadow_runs",
        ["agent_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "coding_shadow_tickets",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("certification_row_id", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "deadline > issued_at AND deadline <= issued_at + interval '2 hours'",
            name="coding_shadow_tickets_deadline_check",
        ),
        sa.CheckConstraint(
            "task_count BETWEEN 1 AND 100",
            name="coding_shadow_tickets_task_count_check",
        ),
        sa.CheckConstraint(
            "validator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$'",
            name="coding_shadow_tickets_validator_check",
        ),
        sa.ForeignKeyConstraint(
            ["run_row_id", "task_count"],
            ["coding_shadow_runs.run_row_id", "coding_shadow_runs.task_count"],
            name="coding_shadow_tickets_run_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["certification_row_id"],
            ["coding_capability_certifications.certification_row_id"],
            name="coding_shadow_tickets_certification_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_id", name="coding_shadow_tickets_pkey"),
        sa.UniqueConstraint(
            "run_row_id",
            "validator_hotkey",
            name="coding_shadow_tickets_run_validator_key",
        ),
        sa.UniqueConstraint(
            "ticket_id",
            "run_row_id",
            "task_count",
            name="coding_shadow_tickets_ticket_run_task_count_key",
        ),
    )
    op.create_index(
        "coding_shadow_tickets_validator_deadline_idx",
        "coding_shadow_tickets",
        ["validator_hotkey", "deadline"],
        unique=False,
    )

    op.create_table(
        "coding_shadow_results",
        sa.Column("result_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("run_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("resolved_count", sa.Integer(), nullable=False),
        sa.Column("repair_failure_count", sa.Integer(), nullable=False),
        sa.Column("infrastructure_count", sa.Integer(), nullable=False),
        sa.Column("invalid_count", sa.Integer(), nullable=False),
        sa.Column("candidate_integrity_count", sa.Integer(), nullable=False),
        sa.Column("control_plane_integrity_count", sa.Integer(), nullable=False),
        sa.Column("scoreable_task_count", sa.Integer(), nullable=False),
        sa.Column("repair_mean_micros", sa.Integer(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_evidence_sha256 ~ '^[0-9a-f]{64}$' AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_shadow_results_integrity_check",
        ),
        sa.CheckConstraint(
            "task_count BETWEEN 1 AND 100 "
            "AND resolved_count >= 0 AND repair_failure_count >= 0 "
            "AND infrastructure_count >= 0 AND invalid_count >= 0 "
            "AND candidate_integrity_count >= 0 "
            "AND control_plane_integrity_count >= 0 "
            "AND scoreable_task_count >= 0 "
            "AND repair_mean_micros BETWEEN 0 AND 1000000",
            name="coding_shadow_results_bounds_check",
        ),
        sa.CheckConstraint(
            "resolved_count + repair_failure_count + infrastructure_count + "
            "invalid_count + candidate_integrity_count + "
            "control_plane_integrity_count = task_count "
            "AND scoreable_task_count = resolved_count + repair_failure_count + "
            "candidate_integrity_count",
            name="coding_shadow_results_counts_check",
        ),
        sa.CheckConstraint(
            "(scoreable_task_count = 0 AND repair_mean_micros = 0) OR "
            "(scoreable_task_count > 0 AND repair_mean_micros = "
            "(resolved_count * 1000000) / scoreable_task_count)",
            name="coding_shadow_results_mean_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_results_weight_ineligible",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            name="coding_shadow_results_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("result_id", name="coding_shadow_results_pkey"),
        sa.UniqueConstraint(
            "ticket_id",
            name="coding_shadow_results_ticket_key",
        ),
    )
    op.create_index(
        "coding_shadow_results_run_created_idx",
        "coding_shadow_results",
        ["run_row_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "coding_shadow_results_run_created_idx",
        table_name="coding_shadow_results",
    )
    op.drop_table("coding_shadow_results")
    op.drop_index(
        "coding_shadow_tickets_validator_deadline_idx",
        table_name="coding_shadow_tickets",
    )
    op.drop_table("coding_shadow_tickets")
    op.drop_index(
        "coding_shadow_runs_agent_created_idx",
        table_name="coding_shadow_runs",
    )
    op.drop_table("coding_shadow_runs")

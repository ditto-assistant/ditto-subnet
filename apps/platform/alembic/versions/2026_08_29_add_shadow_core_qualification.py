"""add append-only shadow core qualification

Revision ID: d8e41a7c3f20
Revises: c7d91f4a2e60
Create Date: 2026-08-29

The policy and observations are diagnostic only. Nothing in the score, rank,
queue, weight, or emissions path reads them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d8e41a7c3f20"
down_revision: str | Sequence[str] | None = "c7d91f4a2e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_qualification_policy_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("enter_composite", sa.Float(), nullable=False),
        sa.Column("enter_tool_mean", sa.Float(), nullable=False),
        sa.Column("enter_memory_mean", sa.Float(), nullable=False),
        sa.Column("exit_composite", sa.Float(), nullable=False),
        sa.Column("exit_tool_mean", sa.Float(), nullable=False),
        sa.Column("exit_memory_mean", sa.Float(), nullable=False),
        sa.Column("enter_observations", sa.Integer(), nullable=False),
        sa.Column("exit_observations", sa.Integer(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bench_version >= 7 AND parent_revision >= 0",
            name="core_qualification_policy_identity_check",
        ),
        sa.CheckConstraint(
            "enter_composite BETWEEN 0 AND 1 "
            "AND enter_tool_mean BETWEEN 0 AND 1 "
            "AND enter_memory_mean BETWEEN 0 AND 1 "
            "AND exit_composite BETWEEN 0 AND 1 "
            "AND exit_tool_mean BETWEEN 0 AND 1 "
            "AND exit_memory_mean BETWEEN 0 AND 1",
            name="core_qualification_policy_score_range_check",
        ),
        sa.CheckConstraint(
            "exit_composite <= enter_composite "
            "AND exit_tool_mean <= enter_tool_mean "
            "AND exit_memory_mean <= enter_memory_mean",
            name="core_qualification_policy_hysteresis_check",
        ),
        sa.CheckConstraint(
            "enter_observations BETWEEN 1 AND 20 "
            "AND exit_observations BETWEEN 1 AND 20",
            name="core_qualification_policy_streak_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="core_qualification_policy_weight_ineligible",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="core_qualification_policy_checksum_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="core_qualification_policy_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="core_qualification_policy_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "bench_version",
            "parent_revision",
            name="core_qualification_policy_parent_key",
        ),
        sa.UniqueConstraint(
            "revision",
            "bench_version",
            "checksum",
            name="core_qualification_policy_binding_key",
        ),
    )
    op.create_index(
        "core_qualification_policy_bench_revision_idx",
        "core_qualification_policy_revisions",
        ["bench_version", "revision"],
        unique=True,
    )

    op.create_table(
        "core_qualification_observations",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("policy_checksum", sa.Text(), nullable=False),
        sa.Column("score_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("score_count", sa.Integer(), nullable=False),
        sa.Column("full_size", sa.Boolean(), nullable=False),
        sa.Column("complete_wave", sa.Boolean(), nullable=False),
        sa.Column(
            "score_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("median_composite", sa.Float(), nullable=False),
        sa.Column("median_tool_mean", sa.Float(), nullable=False),
        sa.Column("median_memory_mean", sa.Float(), nullable=False),
        sa.Column("entry_passed", sa.Boolean(), nullable=False),
        sa.Column("retention_passed", sa.Boolean(), nullable=False),
        sa.Column("qualified", sa.Boolean(), nullable=False),
        sa.Column("enter_streak", sa.Integer(), nullable=False),
        sa.Column("exit_streak", sa.Integer(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "observed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND policy_checksum ~ '^[0-9a-f]{64}$' "
            "AND score_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="core_qualification_observations_hashes_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 7 AND score_count >= 3",
            name="core_qualification_observations_score_count_check",
        ),
        sa.CheckConstraint(
            "median_composite BETWEEN 0 AND 1 "
            "AND median_tool_mean BETWEEN 0 AND 1 "
            "AND median_memory_mean BETWEEN 0 AND 1",
            name="core_qualification_observations_score_range_check",
        ),
        sa.CheckConstraint(
            "enter_streak BETWEEN 0 AND 20 AND exit_streak BETWEEN 0 AND 20",
            name="core_qualification_observations_streak_check",
        ),
        sa.CheckConstraint(
            "decision IN ('partial_wave', 'below_entry', 'pending_entry', 'entered', "
            "'held', 'pending_exit', 'exited')",
            name="core_qualification_observations_decision_check",
        ),
        sa.CheckConstraint(
            "(complete_wave = false AND decision = 'partial_wave') OR "
            "(complete_wave = true AND decision <> 'partial_wave')",
            name="core_qualification_observations_wave_shape_check",
        ),
        sa.CheckConstraint(
            "decision = 'partial_wave' OR "
            "(qualified = true AND decision IN ('entered', 'held', 'pending_exit')) "
            "OR (qualified = false AND decision IN "
            "('below_entry', 'pending_entry', 'exited'))",
            name="core_qualification_observations_decision_state_check",
        ),
        sa.CheckConstraint(
            "(source = 'score_commit' AND actor IS NULL AND reason IS NULL) OR "
            "(source = 'admin_refresh' AND length(trim(actor)) BETWEEN 1 AND 120 "
            "AND length(trim(reason)) >= 8)",
            name="core_qualification_observations_source_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="core_qualification_observations_weight_ineligible",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="core_qualification_observations_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision", "bench_version", "policy_checksum"],
            [
                "core_qualification_policy_revisions.revision",
                "core_qualification_policy_revisions.bench_version",
                "core_qualification_policy_revisions.checksum",
            ],
            name="core_qualification_observations_policy_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "policy_revision",
            "score_evidence_sha256",
            name="core_qualification_observations_evidence_key",
        ),
        sa.UniqueConstraint(
            "sequence",
            name="core_qualification_observations_sequence_key",
        ),
    )
    op.create_index(
        "core_qualification_observations_agent_bench_sequence_idx",
        "core_qualification_observations",
        ["agent_id", "bench_version", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "core_qualification_observations_agent_bench_sequence_idx",
        table_name="core_qualification_observations",
    )
    op.drop_table("core_qualification_observations")
    op.drop_index(
        "core_qualification_policy_bench_revision_idx",
        table_name="core_qualification_policy_revisions",
    )
    op.drop_table("core_qualification_policy_revisions")

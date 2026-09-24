"""Separate exact-attempt report-only L2 canaries from screening authority.

Revision ID: e9c24a7b135d
Revises: b87d2e4f10a9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e9c24a7b135d"
down_revision: str | Sequence[str] | None = "b87d2e4f10a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_l2_report_canaries",
        sa.Column("canary_id", sa.UUID(), primary_key=True),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("source_attempt_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("target_node_id", sa.Text(), nullable=False),
        sa.Column("expected_agent_status", sa.Text(), nullable=False),
        sa.Column("expected_score_count", sa.Integer(), nullable=False),
        sa.Column("review_label", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("claimed_instance_id", sa.Text()),
        sa.Column("settings_revision", sa.Integer()),
        sa.Column("settings_checksum", sa.Text()),
        sa.Column("runtime_evidence_sha256", sa.Text()),
        sa.Column("lease_token_hash", sa.Text()),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "report",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        ),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["target_node_id"], ["screener_nodes.node_id"]),
        sa.UniqueConstraint("request_id", name="screener_l2_canary_request_key"),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'", name="screener_l2_canary_sha_check"
        ),
        sa.CheckConstraint(
            "policy_version = 13 AND bench_version = 13",
            name="screener_l2_canary_v13_check",
        ),
        sa.CheckConstraint(
            "expected_score_count >= 0", name="screener_l2_canary_scores_check"
        ),
        sa.CheckConstraint(
            "review_label IN ('candidate_clear', 'known_reject')",
            name="screener_l2_canary_label_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'incomplete', 'expired')",
            name="screener_l2_canary_status_check",
        ),
        sa.CheckConstraint(
            "lease_token_hash IS NULL OR lease_token_hash ~ '^[0-9a-f]{64}$'",
            name="screener_l2_canary_token_check",
        ),
        sa.CheckConstraint(
            "runtime_evidence_sha256 IS NULL OR "
            "runtime_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="screener_l2_canary_runtime_check",
        ),
    )
    op.create_index(
        "screener_l2_canary_queue_idx",
        "screener_l2_report_canaries",
        ["target_node_id", "status", "created_at"],
    )
    op.create_index(
        "screener_l2_canary_one_active_source_idx",
        "screener_l2_report_canaries",
        ["source_attempt_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'leased')"),
    )


def downgrade() -> None:
    op.drop_index(
        "screener_l2_canary_one_active_source_idx",
        table_name="screener_l2_report_canaries",
    )
    op.drop_index(
        "screener_l2_canary_queue_idx", table_name="screener_l2_report_canaries"
    )
    op.drop_table("screener_l2_report_canaries")

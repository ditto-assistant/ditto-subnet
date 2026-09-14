"""Policy v13 screening decision records (review_timed_out finalizer).

Revision ID: 5e1f7a9c2b04
Revises: e6f4a9c2d781
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5e1f7a9c2b04"
down_revision = "e6f4a9c2d781"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per terminal review decision: operator clear/reject through
    # resolve_ath_review, or the Platform finalizer's no-fault
    # review_timed_out. Reason/evidence text is unbounded on purpose.
    op.create_table(
        "screening_decision_records",
        sa.Column("decision_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("quarantine_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("review_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("violation_proven", sa.Boolean(), nullable=False),
        sa.Column("failure_domain", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("independent_workers", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("identities", postgresql.JSONB(), nullable=False),
        sa.Column("review_scope", sa.Text(), nullable=True),
        sa.Column("completed_checks", postgresql.JSONB(), nullable=False),
        sa.Column("failed_checks", postgresql.JSONB(), nullable=False),
        sa.Column("opaque_components", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_references", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=True),
        sa.Column("limitations", postgresql.JSONB(), nullable=False),
        sa.Column("public_reason", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("supersedes_decision", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("operator_override", postgresql.JSONB(), nullable=True),
        sa.Column(
            "precedent_weight",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("retry_grant_id", sa.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="SET NULL",
            name="screening_decision_records_attempt_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["quarantine_id"],
            ["screening_quarantines.quarantine_id"],
            ondelete="SET NULL",
            name="screening_decision_records_quarantine_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["ath_reviews.review_id"],
            ondelete="SET NULL",
            name="screening_decision_records_review_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision"],
            ["screening_decision_records.decision_id"],
            ondelete="SET NULL",
            name="screening_decision_records_supersedes_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["retry_grant_id"],
            ["screening_retry_overrides.override_id"],
            ondelete="SET NULL",
            name="screening_decision_records_retry_grant_fkey",
        ),
        sa.CheckConstraint(
            "outcome IN ('clear', 'reject', 'review_timed_out')",
            name="screening_decision_records_outcome_check",
        ),
        sa.CheckConstraint(
            "failure_domain IN ('artifact', 'submission', 'platform', "
            "'provider', 'none')",
            name="screening_decision_records_failure_domain_check",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND independent_workers >= 0",
            name="screening_decision_records_retry_evidence_check",
        ),
        sa.CheckConstraint(
            "policy_version > 0",
            name="screening_decision_records_policy_version_check",
        ),
        sa.CheckConstraint(
            "outcome <> 'review_timed_out' OR "
            "(violation_proven = false AND precedent_weight = false)",
            name="screening_decision_records_timeout_no_fault_check",
        ),
        sa.CheckConstraint(
            "length(trim(reviewer)) BETWEEN 1 AND 120",
            name="screening_decision_records_reviewer_check",
        ),
    )
    op.create_index(
        "screening_decision_records_agent_decided_idx",
        "screening_decision_records",
        ["agent_id", "decided_at", "decision_id"],
    )
    op.create_index(
        "screening_decision_records_outcome_decided_idx",
        "screening_decision_records",
        ["outcome", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screening_decision_records_outcome_decided_idx",
        table_name="screening_decision_records",
    )
    op.drop_index(
        "screening_decision_records_agent_decided_idx",
        table_name="screening_decision_records",
    )
    op.drop_table("screening_decision_records")

"""Add append-only scored screening snapshot restoration audit."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8a4f6b2c913"
down_revision: str | Sequence[str] | None = "b5c8e1f37a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scored_screening_snapshot_restorations",
        sa.Column("restoration_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("displaced_attempt_id", sa.UUID(), nullable=False),
        sa.Column("restored_attempt_id", sa.UUID(), nullable=False),
        sa.Column("source_activation_revision", sa.Integer(), nullable=False),
        sa.Column("current_activation_revision", sa.Integer(), nullable=False),
        sa.Column("source_policy_version", sa.Integer(), nullable=False),
        sa.Column("target_policy_version", sa.Integer(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.Text(), nullable=False),
        sa.Column("previous_policy_version", sa.Integer(), nullable=False),
        sa.Column("restored_policy_version", sa.Integer(), nullable=False),
        sa.Column("score_count", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("restoration_id"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="scored_screening_restorations_agent_id_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["displaced_attempt_id"],
            ["screening_attempts.attempt_id"],
            name="scored_screening_restorations_displaced_attempt_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["restored_attempt_id"],
            ["screening_attempts.attempt_id"],
            name="scored_screening_restorations_restored_attempt_fkey",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_policy_version > target_policy_version",
            name="scored_screening_restorations_policy_order_check",
        ),
        sa.CheckConstraint(
            "restored_policy_version <= target_policy_version",
            name="scored_screening_restorations_restored_policy_check",
        ),
        sa.CheckConstraint(
            "bench_version > 0 AND score_count >= 3",
            name="scored_screening_restorations_score_check",
        ),
        sa.CheckConstraint(
            "previous_status IN ('screening_failed', 'rejected')",
            name="scored_screening_restorations_previous_status_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="scored_screening_restorations_actor_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="scored_screening_restorations_reason_check",
        ),
        sa.UniqueConstraint(
            "displaced_attempt_id",
            name="scored_screening_restorations_displaced_attempt_key",
        ),
    )
    op.create_index(
        "scored_screening_restorations_batch_idx",
        "scored_screening_snapshot_restorations",
        ["batch_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "scored_screening_restorations_batch_idx",
        table_name="scored_screening_snapshot_restorations",
    )
    op.drop_table("scored_screening_snapshot_restorations")

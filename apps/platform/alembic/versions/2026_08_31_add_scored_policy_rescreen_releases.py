"""Gate scored policy rescreens behind explicit operator releases.

Revision ID: 6ec0a1d5b814
Revises: c8e2a5d7f491
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6ec0a1d5b814"
down_revision: str | Sequence[str] | None = "c8e2a5d7f491"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scored_policy_rescreen_releases",
        sa.Column("release_id", sa.UUID(), nullable=False),
        sa.Column("activation_revision", sa.Integer(), nullable=False),
        sa.Column("target_policy_version", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("release_id"),
        sa.ForeignKeyConstraint(
            ["activation_revision"],
            ["screener_policy_activations.revision"],
            ondelete="RESTRICT",
            name="scored_policy_rescreen_releases_activation_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="scored_policy_rescreen_releases_agent_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
            name="scored_policy_rescreen_releases_attempt_fkey",
        ),
        sa.CheckConstraint(
            "target_policy_version >= 1 AND position >= 1",
            name="scored_policy_rescreen_releases_position_check",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'paused', 'terminal')",
            name="scored_policy_rescreen_releases_state_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="scored_policy_rescreen_releases_actor_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="scored_policy_rescreen_releases_reason_check",
        ),
        sa.UniqueConstraint(
            "activation_revision",
            "agent_id",
            name="scored_policy_rescreen_releases_activation_agent_key",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            name="scored_policy_rescreen_releases_attempt_key",
        ),
    )
    op.create_index(
        "scored_policy_rescreen_releases_active_idx",
        "scored_policy_rescreen_releases",
        ["activation_revision", "target_policy_version", "state", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "scored_policy_rescreen_releases_active_idx",
        table_name="scored_policy_rescreen_releases",
    )
    op.drop_table("scored_policy_rescreen_releases")

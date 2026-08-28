"""Add scheduled screening-policy activations.

Screening policy text ships with the build, but the version the queue REQUIRES
is a subnet decision with a fairness timeline: miners get equal notice because
the newest text activates on a schedule instead of at deploy time. This table
stores the append-only schedule; until an activation is due the platform
requires the floor version and dual-text workers screen under it, and when
``activate_at`` passes the required version rises and every agent screened
under a stale version re-enters the screening queue.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c3f6a9e2b514"
down_revision: str | Sequence[str] | None = "8d2e1f4c9a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_policy_activations",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("target_policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "activate_at", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("rescreen_scored", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.CheckConstraint(
            "target_policy_version >= 1",
            name="screener_policy_activation_target_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="screener_policy_activation_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_policy_activation_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_policy_activation_actor_check",
        ),
        sa.UniqueConstraint(
            "parent_revision",
            name="screener_policy_activation_parent_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("screener_policy_activations")

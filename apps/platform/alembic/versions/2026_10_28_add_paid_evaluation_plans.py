"""Add cycle-keyed paid evaluation plans and immutable confirmation payer binding.

Revision ID: 5a7c9e1b3d62
Revises: 3c5e7a9b2d41
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5a7c9e1b3d62"
down_revision = "3c5e7a9b2d41"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paid_evaluation_plans",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column(
            "bundle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("confirmation_bundles.bundle_id"),
        ),
        sa.Column("payer_uid", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("estimate_microusd", sa.BigInteger(), nullable=False),
        sa.Column("cap_microusd", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "phase IN ('competition','confirmation')", name="paid_eval_phase"
        ),
        sa.CheckConstraint(
            "state IN ('proposed','approved','closing','closed')",
            name="paid_eval_state",
        ),
        sa.CheckConstraint(
            "cycle>0 AND estimate_microusd>0 AND cap_microusd>=estimate_microusd",
            name="paid_eval_bounds",
        ),
        sa.CheckConstraint(
            "(phase='competition' AND bundle_id IS NULL) OR "
            "(phase='confirmation' AND bundle_id IS NOT NULL)",
            name="paid_eval_phase_bundle",
        ),
        sa.UniqueConstraint("agent_id", "bench_version", "phase", "cycle"),
    )
    op.create_index(
        "paid_eval_active_agent",
        "paid_evaluation_plans",
        ["agent_id", "bench_version", "phase"],
        unique=True,
        postgresql_where=sa.text("state<>'closed'"),
    )
    op.create_index(
        "paid_eval_active_bundle",
        "paid_evaluation_plans",
        ["bundle_id"],
        unique=True,
        postgresql_where=sa.text("state<>'closed' AND bundle_id IS NOT NULL"),
    )
    op.create_table(
        "paid_evaluation_grant_bindings",
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paid_evaluation_plans.run_id"),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("paid_evaluation_grant_bindings")
    op.drop_table("paid_evaluation_plans")

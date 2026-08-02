"""add reusable credits for accidental identical payments

Revision ID: b9e5d7f31c42
Revises: a4f7c9d21e60
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b9e5d7f31c42"
down_revision: str | None = "a4f7c9d21e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("evaluation_payments", "agent_id", nullable=True)
    op.add_column(
        "evaluation_payments",
        sa.Column("credit_for_agent_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "evaluation_payments_credit_for_agent_id_fkey",
        "evaluation_payments",
        "agents",
        ["credit_for_agent_id"],
        ["agent_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "evaluation_payments_assignment_xor_credit",
        "evaluation_payments",
        "(agent_id IS NOT NULL) <> (credit_for_agent_id IS NOT NULL)",
    )
    op.create_index(
        "evaluation_payments_available_credit_idx",
        "evaluation_payments",
        ["miner_hotkey"],
        unique=False,
        postgresql_where=sa.text("agent_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "evaluation_payments_available_credit_idx",
        table_name="evaluation_payments",
    )
    op.drop_constraint(
        "evaluation_payments_assignment_xor_credit",
        "evaluation_payments",
        type_="check",
    )
    op.drop_constraint(
        "evaluation_payments_credit_for_agent_id_fkey",
        "evaluation_payments",
        type_="foreignkey",
    )
    # The old schema has nowhere to represent an unconsumed credit. A downgrade
    # intentionally drops only those unassigned rows; already-consumed payments
    # retain their original proof and agent binding.
    op.execute("DELETE FROM evaluation_payments WHERE agent_id IS NULL")
    op.drop_column("evaluation_payments", "credit_for_agent_id")
    op.alter_column("evaluation_payments", "agent_id", nullable=False)

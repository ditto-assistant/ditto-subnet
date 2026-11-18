"""Add the model_context operator action audit.

One append-only row per operator write on the model_context (CPM)
competition that is not itself a settings revision: a forced season
rotation, a rescore, a rejection. Backroom serves it with the submission it
names, so the audit trail never lives only in a UI. A new table, no hot table
touched, no live behavior changed.

Revision ID: 5c2b75e9c0b4
Revises: 8e3b6f1a2c94
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5c2b75e9c0b4"
down_revision: str | Sequence[str] | None = "8e3b6f1a2c94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_context_operator_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('rotate_season', 'rescore', 'reject')",
            name="model_context_operator_actions_action",
        ),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 120",
            name="model_context_operator_actions_subject",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="model_context_operator_actions_actor",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="model_context_operator_actions_reason",
        ),
    )
    op.create_index(
        "model_context_operator_actions_subject_idx",
        "model_context_operator_actions",
        ["subject", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("model_context_operator_actions")

"""record operator retries on dueling slice preparations

Revision ID: fd7cc6236807
Revises: b7d3e91c4f20
Create Date: 2026-11-03

A failed dueling slice preparation is terminal until an operator re-queues it
(``retry_dueling_slice_preparation``). ``retries`` counts those re-queues so the
lease's attempt cap restarts for each one while ``attempts`` keeps the whole
history; ``last_retry_*`` name who re-queued it, when and why (the full audit
row is in ``generator_operator_actions``). The table is small and off the
request hot path, so plain column adds are fine.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "fd7cc6236807"
down_revision: str | Sequence[str] | None = "b7d3e91c4f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "dueling_slice_preparations"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(TABLE, sa.Column("last_retry_at", sa.TIMESTAMP(timezone=True)))
    op.add_column(TABLE, sa.Column("last_retry_actor", sa.Text()))
    op.add_column(TABLE, sa.Column("last_retry_reason", sa.Text()))
    op.create_check_constraint(
        "dueling_slice_preparations_retries_check",
        TABLE,
        "retries >= 0 AND (retries = 0) = (last_retry_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("dueling_slice_preparations_retries_check", TABLE, type_="check")
    op.drop_column(TABLE, "last_retry_reason")
    op.drop_column(TABLE, "last_retry_actor")
    op.drop_column(TABLE, "last_retry_at")
    op.drop_column(TABLE, "retries")

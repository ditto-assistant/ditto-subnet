"""add agent_kingship (write-once first-coronation ledger for king-only release)

Revision ID: d51a7c9e28f4
Revises: b3f9a1c72e40
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d51a7c9e28f4"
down_revision: str | None = "b3f9a1c72e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_kingship",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column(
            "first_crowned_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_kingship")

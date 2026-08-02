"""add agent_kingship.weight_confirmed_at (on-chain weight gate for king release)

Revision ID: e7a2c4b91d63
Revises: d51a7c9e28f4
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7a2c4b91d63"
down_revision: str | None = "d51a7c9e28f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_kingship",
        sa.Column(
            "weight_confirmed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_kingship", "weight_confirmed_at")

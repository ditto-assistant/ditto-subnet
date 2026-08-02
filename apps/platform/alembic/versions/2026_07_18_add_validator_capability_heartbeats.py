"""add signed validator capability heartbeats

Revision ID: f7c8d9e0a1b2
Revises: f3a7c9d2e4b1
Create Date: 2026-07-18 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f7c8d9e0a1b2"
down_revision: str | Sequence[str] | None = "f3a7c9d2e4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "validator_heartbeats", sa.Column("capabilities", json_type, nullable=True)
    )
    op.add_column("validator_heartbeats", sa.Column("stack", json_type, nullable=True))


def downgrade() -> None:
    op.drop_column("validator_heartbeats", "stack")
    op.drop_column("validator_heartbeats", "capabilities")

"""add signed per-component validator stack health (heartbeat v9)

Revision ID: a1d4f8b6c9e2
Revises: e9b7c4a12d63
Create Date: 2026-07-19 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a1d4f8b6c9e2"
down_revision: str | Sequence[str] | None = "e9b7c4a12d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "validator_heartbeats", sa.Column("stack_health", json_type, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("validator_heartbeats", "stack_health")

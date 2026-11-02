"""Persist optional text-free completed L4 telemetry on its attempt row.

Revision ID: 6d89f7c2ab40
Revises: 8a4d27c0f639
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6d89f7c2ab40"
down_revision: str | Sequence[str] | None = "c7e49af025ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_quarantines",
        sa.Column(
            "court_completion_receipt",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("screening_quarantines", "court_completion_receipt")

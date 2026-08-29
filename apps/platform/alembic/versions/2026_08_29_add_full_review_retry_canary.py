"""add exact full-review screening retry canary

Revision ID: e4c7a1d9b260
Revises: d8a4f6b2c913
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4c7a1d9b260"
down_revision: str | Sequence[str] | None = "d8a4f6b2c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_retry_overrides",
        sa.Column(
            "force_full_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("screening_retry_overrides", "force_full_review")

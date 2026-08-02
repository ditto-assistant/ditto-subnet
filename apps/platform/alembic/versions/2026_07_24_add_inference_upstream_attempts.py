"""add inference upstream attempt accounting

Revision ID: d91f3a7b2c10
Revises: c6e9a1d47b20
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d91f3a7b2c10"
down_revision: str | None = "c6e9a1d47b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inference_requests",
        sa.Column(
            "upstream_attempts", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.create_check_constraint(
        "inference_requests_upstream_attempts",
        "inference_requests",
        "upstream_attempts >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "inference_requests_upstream_attempts",
        "inference_requests",
        type_="check",
    )
    op.drop_column("inference_requests", "upstream_attempts")

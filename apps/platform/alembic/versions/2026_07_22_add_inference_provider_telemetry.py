"""add body-free upstream provider telemetry

Revision ID: a4f7c9d21e60
Revises: d8b02a15e3cf
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4f7c9d21e60"
down_revision: str | None = "d8b02a15e3cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inference_requests",
        sa.Column("upstream_provider", sa.Text(), nullable=True),
    )
    op.add_column(
        "inference_requests",
        sa.Column("timed_out", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "inference_requests",
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inference_requests", "latency_ms")
    op.drop_column("inference_requests", "timed_out")
    op.drop_column("inference_requests", "upstream_provider")

"""add inference recovery telemetry

Revision ID: a8c9d0e1f2b3
Revises: f7b8c9d0e1a2
Create Date: 2026-08-03 01:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c9d0e1f2b3"
down_revision: str | Sequence[str] | None = "f7b8c9d0e1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inference_requests",
        sa.Column(
            "openrouter_attempts", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "inference_requests",
        sa.Column("fallback_phase", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "inference_requests",
        sa.Column("terminal_error_code", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "inference_requests_openrouter_attempts",
        "inference_requests",
        "openrouter_attempts >= 0",
    )
    op.create_check_constraint(
        "inference_requests_fallback_phase",
        "inference_requests",
        "fallback_phase BETWEEN 0 AND 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "inference_requests_fallback_phase",
        "inference_requests",
        type_="check",
    )
    op.drop_constraint(
        "inference_requests_openrouter_attempts",
        "inference_requests",
        type_="check",
    )
    op.drop_column("inference_requests", "terminal_error_code")
    op.drop_column("inference_requests", "fallback_phase")
    op.drop_column("inference_requests", "openrouter_attempts")

"""Keep full-runtime report canaries separate from source-only replays.

Revision ID: f9c3a2b671d4
Revises: d18b0f5a72c9
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9c3a2b671d4"
down_revision: str | Sequence[str] | None = "d18b0f5a72c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screener_l2_report_canaries",
        sa.Column("run_mode", sa.Text(), nullable=False, server_default="source_only"),
    )
    op.create_check_constraint(
        "screener_l2_canary_run_mode_check",
        "screener_l2_report_canaries",
        "run_mode IN ('source_only', 'full_runtime')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screener_l2_canary_run_mode_check",
        "screener_l2_report_canaries",
    )
    op.drop_column("screener_l2_report_canaries", "run_mode")

"""Record current-object provenance for report-only historical L2 replays.

Revision ID: a2f4d9c51e60
Revises: 5d1f7a93c2e8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2f4d9c51e60"
down_revision: str | Sequence[str] | None = "5d1f7a93c2e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screener_l2_report_canaries",
        sa.Column(
            "source_attestation",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("screener_l2_report_canaries", "source_attestation")

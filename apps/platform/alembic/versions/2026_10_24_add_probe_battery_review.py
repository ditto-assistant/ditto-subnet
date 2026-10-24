"""add the report-only probe reviewer summary to screener probe batteries

Revision ID: 8b3d6f0c2a71
Revises: 5c1e9a7d2b40
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8b3d6f0c2a71"
down_revision: str | Sequence[str] | None = "5c1e9a7d2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # screener_probe_batteries is a new, low-write telemetry table: a plain
    # nullable add is metadata-only and needs no batched backfill.
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "screener_probe_batteries", sa.Column("review", json_type, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("screener_probe_batteries", "review")

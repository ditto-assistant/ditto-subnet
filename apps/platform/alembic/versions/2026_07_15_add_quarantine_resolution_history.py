"""add append-only quarantine resolution history

Revision ID: b42e9c5d1f83
Revises: a31d8b4c9e72
Create Date: 2026-07-15 04:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b42e9c5d1f83"
down_revision: str | Sequence[str] | None = "a31d8b4c9e72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE screening_quarantine_resolutions (
            resolution_id UUID PRIMARY KEY,
            quarantine_id UUID NOT NULL REFERENCES screening_quarantines(quarantine_id)
                ON DELETE CASCADE,
            resolution TEXT NOT NULL CHECK (
                resolution IN ('release','rescreen','reject')
            ),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX screening_quarantine_resolutions_quarantine_created_idx "
        "ON screening_quarantine_resolutions(quarantine_id, created_at)"
    )
    op.execute(
        """
        INSERT INTO screening_quarantine_resolutions (
            resolution_id, quarantine_id, resolution, reason, actor, created_at
        )
        SELECT quarantine_id, quarantine_id, resolution,
               COALESCE(resolution_reason, 'Legacy operator resolution'),
               COALESCE(resolved_by, 'legacy:unknown'),
               COALESCE(resolved_at, created_at)
        FROM screening_quarantines
        WHERE status = 'resolved' AND resolution IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screening_quarantine_resolutions")

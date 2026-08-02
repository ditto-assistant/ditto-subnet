"""add one-time miner screening disputes

Revision ID: c53fa6d2b194
Revises: c73a1e5f9b24
Create Date: 2026-07-15 05:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c53fa6d2b194"
down_revision: str | Sequence[str] | None = "c73a1e5f9b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE screening_disputes (
            dispute_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL UNIQUE REFERENCES agents(agent_id) ON DELETE CASCADE,
            quarantine_id UUID NOT NULL UNIQUE
                REFERENCES screening_quarantines(quarantine_id) ON DELETE CASCADE,
            miner_hotkey TEXT NOT NULL,
            message TEXT NOT NULL CHECK (length(message) BETWEEN 20 AND 1000),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','resolved')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            resolved_by TEXT,
            resolution TEXT CHECK (
                resolution IS NULL OR resolution IN ('release','uphold')
            ),
            resolution_reason TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX screening_disputes_status_created_idx "
        "ON screening_disputes(status, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screening_disputes")

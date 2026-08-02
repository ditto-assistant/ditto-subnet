"""add persistent screening quarantines

Revision ID: e7c4a18f2b61
Revises: d4f8b2e61a90
Create Date: 2026-07-14 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7c4a18f2b61"
down_revision: str | Sequence[str] | None = "d4f8b2e61a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE agentstatus ADD VALUE IF NOT EXISTS 'quarantined'")
    op.execute(
        "ALTER TABLE screening_attempts DROP CONSTRAINT screening_attempts_status_check"
    )
    op.execute(
        "ALTER TABLE screening_attempts ADD CONSTRAINT "
        "screening_attempts_status_check CHECK (status IN "
        "('running','passed','rejected','failed','expired','quarantined'))"
    )
    op.execute(
        """
        CREATE TABLE screening_quarantines (
            quarantine_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
            attempt_id UUID NOT NULL UNIQUE
                REFERENCES screening_attempts(attempt_id) ON DELETE CASCADE,
            screener_hotkey TEXT NOT NULL,
            policy_version INTEGER NOT NULL CHECK (policy_version > 0),
            manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
            finding_digest TEXT CHECK (
                finding_digest IS NULL OR finding_digest ~ '^[0-9a-f]{64}$'
            ),
            reason_code TEXT NOT NULL CHECK (
                reason_code ~ '^[a-z0-9][a-z0-9-]{0,63}$'
            ),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','resolved')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            resolved_by TEXT,
            resolution TEXT CHECK (
                resolution IS NULL OR resolution IN ('release','rescreen','reject')
            ),
            resolution_reason TEXT
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX screening_quarantines_one_active_agent_idx "
        "ON screening_quarantines(agent_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX screening_quarantines_created_idx "
        "ON screening_quarantines(created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screening_quarantines")
    op.execute(
        "ALTER TABLE screening_attempts DROP CONSTRAINT screening_attempts_status_check"
    )
    op.execute(
        "ALTER TABLE screening_attempts ADD CONSTRAINT "
        "screening_attempts_status_check CHECK (status IN "
        "('running','passed','rejected','failed','expired'))"
    )
    # PostgreSQL enum values are intentionally not removed during downgrade.

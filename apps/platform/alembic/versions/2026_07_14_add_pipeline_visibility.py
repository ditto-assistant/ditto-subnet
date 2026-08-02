"""add leased screening attempts and active validator work

Revision ID: b3d9e7a14c62
Revises: a9e7c4d21b80
Create Date: 2026-07-14 05:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b3d9e7a14c62"
down_revision: str | Sequence[str] | None = "a9e7c4d21b80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A deterministic screening verdict is not an infrastructure failure.
    op.execute("ALTER TYPE agentstatus ADD VALUE IF NOT EXISTS 'rejected'")
    op.execute(
        """
        CREATE TABLE screening_attempts (
            attempt_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
            screener_hotkey TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            public_reason TEXT,
            CONSTRAINT screening_attempts_policy_version_check
                CHECK (policy_version > 0),
            CONSTRAINT screening_attempts_status_check
                CHECK (
                    status IN ('running', 'passed', 'rejected', 'failed', 'expired')
                ),
            CONSTRAINT screening_attempts_deadline_check
                CHECK (deadline >= started_at),
            CONSTRAINT screening_attempts_finished_check
                CHECK (finished_at IS NULL OR finished_at >= started_at)
        )
        """
    )
    op.execute(
        "CREATE INDEX screening_attempts_agent_started_idx "
        "ON screening_attempts (agent_id, started_at DESC)"
    )
    op.execute(
        "CREATE UNIQUE INDEX screening_attempts_one_running_idx "
        "ON screening_attempts (agent_id) WHERE status = 'running'"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats ADD COLUMN active_agent_id UUID "
        "REFERENCES agents(agent_id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX validator_heartbeats_active_agent_idx "
        "ON validator_heartbeats (active_agent_id) "
        "WHERE active_agent_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS validator_heartbeats_active_agent_idx")
    op.execute("ALTER TABLE validator_heartbeats DROP COLUMN IF EXISTS active_agent_id")
    op.execute("DROP TABLE IF EXISTS screening_attempts")
    # PostgreSQL enum values cannot be removed safely in-place. Leaving the
    # additive value is harmless when rolling application code backward.

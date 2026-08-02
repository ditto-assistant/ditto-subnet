"""add private validator benchmark progress

Revision ID: d4f8b2e61a90
Revises: d8f1b6c24a70
Create Date: 2026-07-14 08:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4f8b2e61a90"
down_revision: str | Sequence[str] | None = "d8f1b6c24a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Private signed payload only. Public endpoints project a six-field allowlist
    # after re-validating the live ticket and coarsening its aggregate counts.
    op.execute("ALTER TABLE validator_heartbeats ADD COLUMN benchmark_progress JSONB")
    op.execute(
        "ALTER TABLE validator_heartbeats ADD COLUMN "
        "benchmark_progress_reported BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats ADD COLUMN benchmark_progress_agent_id UUID"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats ADD CONSTRAINT "
        "validator_heartbeats_benchmark_progress_agent_id_fkey "
        "FOREIGN KEY (benchmark_progress_agent_id) REFERENCES agents(agent_id) "
        "ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE validator_heartbeats DROP CONSTRAINT IF EXISTS "
        "validator_heartbeats_benchmark_progress_agent_id_fkey"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats DROP COLUMN IF EXISTS "
        "benchmark_progress_agent_id"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats DROP COLUMN IF EXISTS "
        "benchmark_progress_reported"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats DROP COLUMN IF EXISTS benchmark_progress"
    )

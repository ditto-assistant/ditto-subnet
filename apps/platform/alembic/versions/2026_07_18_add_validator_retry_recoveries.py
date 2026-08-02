"""add audited validator retry recoveries

Revision ID: d2e5f7a9c1b3
Revises: c91f4e7a2b60
Create Date: 2026-07-18 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d2e5f7a9c1b3"
down_revision: str | Sequence[str] | None = "c91f4e7a2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE validator_tickets "
        "ADD COLUMN manual_retry_grants INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE validator_tickets ADD CONSTRAINT "
        "validator_tickets_manual_retry_grants_nonnegative "
        "CHECK (manual_retry_grants >= 0)"
    )
    op.execute(
        """
        CREATE TABLE validator_retry_recoveries (
            recovery_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            expected_snapshot TEXT NOT NULL,
            score_count INTEGER NOT NULL,
            bench_version INTEGER NOT NULL,
            ticket_snapshot JSONB NOT NULL,
            granted_validator_hotkeys JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT validator_retry_recoveries_agent_id_fkey
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
                ON DELETE RESTRICT,
            CONSTRAINT validator_retry_recoveries_actor_length
                CHECK (length(trim(actor)) BETWEEN 1 AND 120),
            CONSTRAINT validator_retry_recoveries_reason_length
                CHECK (length(trim(reason)) BETWEEN 3 AND 500),
            CONSTRAINT validator_retry_recoveries_score_count_nonnegative
                CHECK (score_count >= 0),
            CONSTRAINT validator_retry_recoveries_bench_version_positive
                CHECK (bench_version > 0),
            CONSTRAINT validator_retry_recoveries_agent_snapshot_key
                UNIQUE (agent_id, bench_version, expected_snapshot)
        )
        """
    )
    op.execute(
        "CREATE INDEX validator_retry_recoveries_agent_created_idx "
        "ON validator_retry_recoveries"
        "(agent_id, bench_version, created_at, recovery_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS validator_retry_recoveries")
    op.execute(
        "ALTER TABLE validator_tickets DROP CONSTRAINT "
        "validator_tickets_manual_retry_grants_nonnegative"
    )
    op.execute("ALTER TABLE validator_tickets DROP COLUMN manual_retry_grants")

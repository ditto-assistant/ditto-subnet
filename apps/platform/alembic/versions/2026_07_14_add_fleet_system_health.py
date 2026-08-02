"""add validator and screener fleet system health

Revision ID: c7e4a91d2b60
Revises: b3d9e7a14c62
Create Date: 2026-07-14 07:15:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7e4a91d2b60"
down_revision: str | Sequence[str] | None = "b3d9e7a14c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE validator_heartbeats "
        "ADD COLUMN first_seen_at TIMESTAMPTZ, "
        "ADD COLUMN system_metrics JSONB"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats "
        "DROP CONSTRAINT validator_heartbeats_state_check"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats ADD CONSTRAINT "
        "validator_heartbeats_state_check CHECK "
        "(state IN ('polling', 'running_benchmark', 'updating_weights', "
        "'idle', 'error', 'paused'))"
    )
    op.execute(
        """
        CREATE TABLE screener_heartbeats (
            screener_hotkey TEXT PRIMARY KEY,
            software_version TEXT NOT NULL,
            protocol_version INTEGER NOT NULL,
            policy_version INTEGER NOT NULL,
            state TEXT NOT NULL,
            active_agent_id UUID REFERENCES agents(agent_id) ON DELETE SET NULL,
            first_seen_at TIMESTAMPTZ,
            system_metrics JSONB,
            reported_at TIMESTAMPTZ NOT NULL,
            seen_at TIMESTAMPTZ NOT NULL,
            signature TEXT NOT NULL,
            CONSTRAINT screener_heartbeats_software_version_length_check
                CHECK (length(software_version) BETWEEN 1 AND 64),
            CONSTRAINT screener_heartbeats_protocol_version_check
                CHECK (protocol_version > 0),
            CONSTRAINT screener_heartbeats_policy_version_check
                CHECK (policy_version > 0),
            CONSTRAINT screener_heartbeats_state_check
                CHECK (state IN ('polling', 'screening', 'error', 'paused')),
            CONSTRAINT screener_heartbeats_signature_length_check
                CHECK (length(signature) = 128)
        )
        """
    )
    op.execute(
        "CREATE INDEX screener_heartbeats_seen_at_idx ON screener_heartbeats (seen_at)"
    )
    op.execute(
        "CREATE INDEX screener_heartbeats_active_agent_idx "
        "ON screener_heartbeats (active_agent_id) "
        "WHERE active_agent_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screener_heartbeats")
    op.execute(
        "ALTER TABLE validator_heartbeats "
        "DROP CONSTRAINT validator_heartbeats_state_check"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats ADD CONSTRAINT "
        "validator_heartbeats_state_check CHECK "
        "(state IN ('polling', 'running_benchmark', 'updating_weights', "
        "'idle', 'error'))"
    )
    op.execute(
        "ALTER TABLE validator_heartbeats "
        "DROP COLUMN IF EXISTS system_metrics, "
        "DROP COLUMN IF EXISTS first_seen_at"
    )

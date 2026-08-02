"""add signed validator software heartbeats

Revision ID: f4a8c2d91e60
Revises: f4c8a1d72e09
Create Date: 2026-07-13 22:45:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4a8c2d91e60"
down_revision: str | Sequence[str] | None = "f4c8a1d72e09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE validator_heartbeats (
            validator_hotkey TEXT PRIMARY KEY,
            software_version TEXT NOT NULL,
            protocol_version INTEGER NOT NULL,
            code_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            reported_at TIMESTAMPTZ NOT NULL,
            seen_at TIMESTAMPTZ NOT NULL,
            signature TEXT NOT NULL,
            CONSTRAINT validator_heartbeats_software_version_length_check
                CHECK (length(software_version) BETWEEN 1 AND 64),
            CONSTRAINT validator_heartbeats_protocol_version_check
                CHECK (protocol_version > 0),
            CONSTRAINT validator_heartbeats_code_digest_length_check
                CHECK (length(code_digest) = 64),
            CONSTRAINT validator_heartbeats_state_check
                CHECK (state IN ('polling', 'running_benchmark',
                    'updating_weights', 'idle', 'error')),
            CONSTRAINT validator_heartbeats_signature_length_check
                CHECK (length(signature) = 128)
        );
        """
    )
    op.execute(
        "CREATE INDEX validator_heartbeats_seen_at_idx "
        "ON validator_heartbeats (seen_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE validator_heartbeats;")

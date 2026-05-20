"""initial schema

Revision ID: 93732e86fe02
Revises:
Create Date: 2026-05-19 13:07:14.633319

Creates the ``agentstatus`` ENUM, the ``agents`` table, and the
``evaluation_payments`` table with its composite-PK replay protection.
Other tables in the eventual 12-table inventory land in their own
migrations alongside the features that consume them.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "93732e86fe02"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ENUM type must be created before the column that references it.
    op.execute(
        """
        CREATE TYPE agentstatus AS ENUM (
            'uploaded',
            'screening',
            'screening_passed',
            'screening_failed',
            'evaluating',
            'scored',
            'live',
            'ath_pending_review',
            'banned'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE agents (
            agent_id      UUID PRIMARY KEY,
            miner_hotkey  TEXT NOT NULL,
            name          TEXT NOT NULL,
            version_num   INTEGER NOT NULL,
            sha256        TEXT NOT NULL,
            status        agentstatus NOT NULL DEFAULT 'uploaded',
            ip_address    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (miner_hotkey, version_num)
        )
        """
    )
    op.execute("CREATE INDEX agents_miner_hotkey_idx ON agents (miner_hotkey)")
    # Validator polls for agents in the 'evaluating' state; a partial index
    # keeps that lookup cheap as the table grows.
    op.execute(
        """
        CREATE INDEX agents_status_evaluating_idx
            ON agents (status)
            WHERE status = 'evaluating'
        """
    )

    # Composite PK is the replay-protection mechanism: the same on-chain
    # payment (identified by block_hash + extrinsic_index) can never be
    # inserted twice.
    op.execute(
        """
        CREATE TABLE evaluation_payments (
            block_hash       TEXT NOT NULL,
            extrinsic_index  INTEGER NOT NULL,
            agent_id         UUID NOT NULL
                REFERENCES agents(agent_id) ON DELETE RESTRICT,
            miner_hotkey     TEXT NOT NULL,
            amount_rao       BIGINT NOT NULL,
            dest_address     TEXT NOT NULL,
            timestamp        TIMESTAMPTZ NOT NULL,
            verified_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (block_hash, extrinsic_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX evaluation_payments_agent_id_idx "
        "ON evaluation_payments (agent_id)"
    )
    op.execute(
        "CREATE INDEX evaluation_payments_miner_hotkey_idx "
        "ON evaluation_payments (miner_hotkey)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS evaluation_payments")
    op.execute("DROP TABLE IF EXISTS agents")
    op.execute("DROP TYPE IF EXISTS agentstatus")

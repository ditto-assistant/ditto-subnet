"""initial schema

Revision ID: 93732e86fe02
Revises:
Create Date: 2026-05-19 13:07:14.633319

Creates the ``agentstatus`` ENUM, the ``agents`` table, and the
``evaluation_payments`` table with its composite-PK replay protection.
Other tables in the eventual table inventory land in their own
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

    # agents.agent_id is the PK. The explicit UNIQUE on (agent_id, miner_hotkey)
    # exists as a FK target so evaluation_payments can enforce that a payment
    # row's miner_hotkey matches the agent it points to (composite FK below).
    op.execute(
        """
        CREATE TABLE agents (
            agent_id      UUID PRIMARY KEY,
            miner_hotkey  TEXT NOT NULL,
            name          TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            status        agentstatus NOT NULL DEFAULT 'uploaded',
            ip_address    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (agent_id, miner_hotkey)
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

    # Composite PK on (block_hash, extrinsic_index) is the replay-protection
    # mechanism: the same on-chain payment cannot be inserted twice. UNIQUE
    # (agent_id) enforces the 1:1 invariant (one upload = one payment) and
    # defends against retry-logic bugs that might double-insert. Composite FK
    # to agents (agent_id, miner_hotkey) documents the ownership invariant in
    # DDL so future endpoints can't silently break it. CHECK constraints catch
    # garbage values that an app-layer bug might let through.
    #
    # The PK below relies on Postgres' default constraint-naming rule to
    # produce the name "evaluation_payments_pkey". The queries layer at
    # ditto.db.queries.payments._PAYMENT_REPLAY_CONSTRAINT dispatches PK
    # violations on that exact string to raise PaymentReplayedError (3207).
    # Any future migration that renames the PK must update the queries
    # constant in lockstep or replay protection silently regresses to a
    # generic IntegrityError 500. The layer-3 integration test
    # test_pk_constraint_name_matches_dispatch_constant catches this drift
    # on every PR.
    op.execute(
        """
        CREATE TABLE evaluation_payments (
            block_hash       TEXT NOT NULL,
            extrinsic_index  INTEGER NOT NULL CHECK (extrinsic_index >= 0),
            agent_id         UUID NOT NULL,
            miner_hotkey     TEXT NOT NULL,
            miner_coldkey    TEXT NOT NULL,
            amount_rao       BIGINT NOT NULL CHECK (amount_rao > 0),
            dest_address     TEXT NOT NULL,
            timestamp        TIMESTAMPTZ NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (block_hash, extrinsic_index),
            UNIQUE (agent_id),
            FOREIGN KEY (agent_id, miner_hotkey)
                REFERENCES agents (agent_id, miner_hotkey)
                ON DELETE RESTRICT
        )
        """
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

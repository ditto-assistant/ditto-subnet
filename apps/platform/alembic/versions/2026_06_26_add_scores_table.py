"""add scores table

Revision ID: 3e6a2ade9b73
Revises: ccd1dcf85ac7
Create Date: 2026-06-26 18:30:00.000000

Creates the ``scores`` table: one row per ``(agent_id, validator_hotkey)``
holding a validator's DittoBench score for an agent. The composite PK keeps
a single current score per validator per agent (re-scores upsert). The
single-column FK to ``agents.agent_id`` cascades on delete because a score
is derived data. Aggregate columns back weight computation + leaderboards;
``details`` JSONB carries the optional per-case breakdown for audit.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3e6a2ade9b73"
down_revision: str | Sequence[str] | None = "ccd1dcf85ac7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``scores`` table + supporting index."""
    op.execute(
        """
        CREATE TABLE scores (
            agent_id          UUID NOT NULL,
            validator_hotkey  TEXT NOT NULL,
            run_id            TEXT NOT NULL,
            seed              BIGINT NOT NULL,
            composite         DOUBLE PRECISION NOT NULL
                                  CHECK (composite >= 0 AND composite <= 1),
            tool_mean         DOUBLE PRECISION NOT NULL
                                  CHECK (tool_mean >= 0 AND tool_mean <= 1),
            memory_mean       DOUBLE PRECISION NOT NULL
                                  CHECK (memory_mean >= 0 AND memory_mean <= 1),
            median_ms         INTEGER NOT NULL CHECK (median_ms >= 0),
            n                 INTEGER NOT NULL CHECK (n >= 0),
            details           JSONB,
            generated_at      TIMESTAMPTZ NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (agent_id, validator_hotkey),
            FOREIGN KEY (agent_id)
                REFERENCES agents (agent_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX scores_agent_id_idx ON scores (agent_id)")


def downgrade() -> None:
    """Drop the ``scores`` table."""
    op.execute("DROP TABLE IF EXISTS scores")

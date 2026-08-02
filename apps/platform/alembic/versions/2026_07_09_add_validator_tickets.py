"""add validator_tickets for k=3 median scoring

Revision ID: b7e1c4a92f30
Revises: a1c9e2b47d53
Create Date: 2026-07-09 12:00:00.000000

Introduces the ticket pool that caps a submission at three independent
validators (the k=3 median-of-three model). The platform issues at most three
``validator_tickets`` per agent, each to a distinct validator hotkey, and
refuses further requests. A ticket is the right to score: the holder loads the
agent + the platform-generated dataset, scores it, and posts a signed score
back before ``deadline``; otherwise the ticket expires and its slot re-opens.

- ``ticketstatus`` PG ENUM (``issued`` / ``scored`` / ``expired``).
- ``validator_tickets`` — PK ``(agent_id, validator_hotkey)`` so a validator
  can hold at most one ticket per agent (it can never take two of three slots).
  ``agent_id`` FKs ``agents.agent_id`` ``ON DELETE CASCADE``.
- Partial index ``validator_tickets_open_idx`` on ``deadline WHERE status =
  'issued'`` for the expiry sweep and the live-slot count.

The three composites live in ``scores`` (one row per ``(agent, validator)``);
this table tracks issuance and lifecycle only.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e1c4a92f30"
down_revision: str | Sequence[str] | None = "a1c9e2b47d53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``ticketstatus`` enum and the ``validator_tickets`` table."""
    op.execute("CREATE TYPE ticketstatus AS ENUM ('issued', 'scored', 'expired')")
    op.execute(
        """
        CREATE TABLE validator_tickets (
            agent_id UUID NOT NULL,
            validator_hotkey TEXT NOT NULL,
            status ticketstatus NOT NULL DEFAULT 'issued',
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deadline TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT validator_tickets_pkey
                PRIMARY KEY (agent_id, validator_hotkey),
            CONSTRAINT validator_tickets_agent_id_fkey
                FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX validator_tickets_agent_id_idx ON validator_tickets (agent_id)"
    )
    op.execute(
        "CREATE INDEX validator_tickets_open_idx "
        "ON validator_tickets (deadline) WHERE status = 'issued'"
    )


def downgrade() -> None:
    """Drop the ``validator_tickets`` table and the ``ticketstatus`` enum."""
    op.execute("DROP TABLE IF EXISTS validator_tickets")
    op.execute("DROP TYPE IF EXISTS ticketstatus")

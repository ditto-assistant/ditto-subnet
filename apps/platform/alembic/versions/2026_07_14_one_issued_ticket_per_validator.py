"""enforce one issued ticket per validator

Revision ID: f1a7c3d92b84
Revises: e7c4a18f2b61
Create Date: 2026-07-14 20:40:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a7c3d92b84"
down_revision: str | Sequence[str] | None = "e7c4a18f2b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older schedulers could leave more than one live lease on one validator.
    # Preserve the lease named by its latest heartbeat where possible; otherwise
    # keep the oldest lease so an already-running benchmark can still submit.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                ticket.agent_id,
                ticket.validator_hotkey,
                row_number() OVER (
                    PARTITION BY ticket.validator_hotkey
                    ORDER BY
                        CASE WHEN heartbeat.active_agent_id = ticket.agent_id
                            THEN 0 ELSE 1 END,
                        ticket.issued_at ASC,
                        ticket.agent_id ASC
                ) AS lease_rank
            FROM validator_tickets AS ticket
            LEFT JOIN validator_heartbeats AS heartbeat
                ON heartbeat.validator_hotkey = ticket.validator_hotkey
            WHERE ticket.status = 'issued'
        )
        UPDATE validator_tickets AS ticket
        SET status = 'expired', retry_after = ticket.deadline + INTERVAL '6 hours'
        FROM ranked
        WHERE ticket.agent_id = ranked.agent_id
          AND ticket.validator_hotkey = ranked.validator_hotkey
          AND ranked.lease_rank > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX validator_tickets_one_issued_per_validator_idx "
        "ON validator_tickets (validator_hotkey) WHERE status = 'issued'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS validator_tickets_one_issued_per_validator_idx")

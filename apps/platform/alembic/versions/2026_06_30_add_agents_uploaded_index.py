"""add agents uploaded partial index

Revision ID: 7c1f2a9d4e50
Revises: 3e6a2ade9b73
Create Date: 2026-06-30 12:00:00.000000

Adds the partial index ``agents_status_uploaded_idx`` (``WHERE status =
'uploaded'``) backing the screener work queue (``GET /screener/queue``), mirroring
``agents_status_evaluating_idx`` for the validator queue. The screener drains
freshly uploaded agents oldest-first; the partial index stays small (only
un-screened rows) and keeps that scan cheap as the table grows.

No enum or column change: the ``agentstatus`` type already carries every
screening state from the initial schema.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1f2a9d4e50"
down_revision: str | Sequence[str] | None = "3e6a2ade9b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the partial index backing the screener queue."""
    op.execute(
        """
        CREATE INDEX agents_status_uploaded_idx
            ON agents (status)
            WHERE status = 'uploaded'
        """
    )


def downgrade() -> None:
    """Drop the screener-queue partial index."""
    op.execute("DROP INDEX IF EXISTS agents_status_uploaded_idx")

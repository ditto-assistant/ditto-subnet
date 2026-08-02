"""drop unused agents.ip_address

Revision ID: ccd1dcf85ac7
Revises: 93732e86fe02
Create Date: 2026-06-09 12:58:55.212202

Drops the ``ip_address`` column from ``agents``. The column was written
on every upload but never read by any code path and has no documented
use case in the design spec or threat model. Removing it shrinks the
retention surface; if a real use case lands later (anti-sybil scoring,
audit forensics in ``upload_attempts``), reintroducing it is a single
migration.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ccd1dcf85ac7"
down_revision: str | Sequence[str] | None = "93732e86fe02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the ``ip_address`` column from ``agents``."""
    op.drop_column("agents", "ip_address")


def downgrade() -> None:
    """Re-add ``ip_address`` as a nullable TEXT column.

    Historical row values are not preserved; the downgrade restores the
    schema shape but the column will be NULL for all pre-downgrade rows.
    """
    op.add_column("agents", sa.Column("ip_address", sa.Text(), nullable=True))

"""record the live leases an operator eviction revoked

Revision ID: b2e9d4a17c60
Revises: e8b3c05d7a41
Create Date: 2026-07-27

``validator_queue_withdrawals`` is an operator-write-only audit table -- a
handful of rows, no hot writers -- so a plain ``add_column`` is correct here and
the ``safe_add_column`` machinery (which exists for ``validator_tickets``,
``inference_grants`` and ``inference_requests``) is not needed.

Nullable on purpose, and left nullable forever: ``NULL`` is the meaningful value
for every row written by the withdrawal route, including the ones already in the
table. A backfill to ``'[]'`` would claim those rows had been eviction-checked
and found nothing live, which is not what happened.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2e9d4a17c60"
down_revision: str | None = "e8b3c05d7a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validator_queue_withdrawals",
        sa.Column(
            "evicted_validator_hotkeys",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("validator_queue_withdrawals", "evicted_validator_hotkeys")

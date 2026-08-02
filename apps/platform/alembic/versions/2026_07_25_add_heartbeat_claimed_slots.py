"""record the slots a validator claims as busy, before ticket confirmation

Revision ID: c4a91b7e2f68
Revises: f1c8d34a7b95
Create Date: 2026-07-25

``validator_heartbeats.benchmark_capacity`` stores only the slots the ledger
could confirm against a live ticket. That makes it unable to distinguish "this
slot is free" from "this slot's progress did not confirm", and the lease
liveness gate reads the former from the latter -- force-expiring a healthy run.
``claimed_slots`` records the signed, unfiltered occupancy claim so the gate has
an honest negative to test against. Nullable and additive: rows written before
this migration simply have no claim, which the gate treats as "no evidence".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4a91b7e2f68"
down_revision: str | Sequence[str] | None = "f1c8d34a7b95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "validator_heartbeats",
        sa.Column("claimed_slots", json_type, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("validator_heartbeats", "claimed_slots")

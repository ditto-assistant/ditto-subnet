"""anchor each generator round to its first chain epoch

Revision ID: 8c41e2f7a9d3
Revises: 18127d681783
Create Date: 2026-10-31

A round's index was ``SubnetEpochIndex // epochs_per_round`` under the
*current* setting, so a cadence change remapped the whole index space: going
from 1 to 4 epochs per round at epoch 26046 makes the current round 6511,
below rounds 26041-26047 that already ran. Every "earlier than the current
round" query (closing stale rounds, revealing past the privacy window) then
stops seeing them, and newest-first reads keep treating them as newest.

Each round now records the subnet epoch it opens at. New indexes continue
from the latest round, whatever the cadence, so the index stays monotonic in
time. Every existing row was keyed by the old rule, so its first epoch is
exactly ``round_index * epochs_per_round``. ``generator_rounds`` is small and
not a hot table: a plain add, backfill and NOT NULL in one transaction.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c41e2f7a9d3"
down_revision: str | Sequence[str] | None = "18127d681783"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "generator_rounds"
_INDEX = "generator_rounds_first_epoch_idx"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("first_epoch_index", sa.BigInteger()))
    op.execute(
        "UPDATE generator_rounds SET first_epoch_index = round_index * epochs_per_round"
    )
    op.alter_column(_TABLE, "first_epoch_index", nullable=False)
    op.create_index(_INDEX, _TABLE, ["first_epoch_index"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "first_epoch_index")

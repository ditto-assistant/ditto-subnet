"""add epoch-pinned validator ledger snapshots

Revision ID: c7a4e19d2f60
Revises: b3f926ce145a
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a4e19d2f60"
down_revision: str | Sequence[str] | None = "b3f926ce145a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "ledger_epoch_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("last_epoch_block", sa.BigInteger(), nullable=False),
        sa.Column("pinned_block", sa.BigInteger(), nullable=False),
        sa.Column("pinned_block_hash", sa.Text(), nullable=False),
        sa.Column("pinned_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("entries", json_type, nullable=False),
        sa.Column("context", json_type, nullable=False),
        sa.Column("champion_agent_id", sa.Uuid(), nullable=True),
        sa.Column("champion_owner_root", sa.Text(), nullable=True),
        sa.Column("incumbent_agent_id", sa.Uuid(), nullable=True),
        sa.Column("ledger_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "netuid", "epoch_index", name="ledger_epoch_snapshots_epoch_key"
        ),
        sa.CheckConstraint(
            "epoch_index >= 0", name="ledger_epoch_snapshots_epoch_index_check"
        ),
        sa.CheckConstraint(
            "pinned_block >= last_epoch_block",
            name="ledger_epoch_snapshots_pinned_block_check",
        ),
        sa.CheckConstraint(
            "length(ledger_digest) = 64",
            name="ledger_epoch_snapshots_digest_check",
        ),
    )
    op.create_index(
        "ledger_epoch_snapshots_recent_idx",
        "ledger_epoch_snapshots",
        ["netuid", sa.text("epoch_index DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ledger_epoch_snapshots_recent_idx", table_name="ledger_epoch_snapshots"
    )
    op.drop_table("ledger_epoch_snapshots")

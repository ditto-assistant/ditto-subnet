"""record which pinned ledger each validator heartbeat folded

Revision ID: a91c3f5e7d24
Revises: c7a4e19d2f60
Create Date: 2026-09-10

The nullable JSONB column preserves every older validator as NULL. It stores
the closed ``WeightsFold`` heartbeat model only: the pinned epoch index and
ledger digest the validator folded, a digest of the weight vector it committed,
and the champion it derived. ``validator_heartbeats`` is hot, so the
metadata-only add/drop uses the bounded-lock migration helpers.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "a91c3f5e7d24"
down_revision: str | Sequence[str] | None = "c7a4e19d2f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_heartbeats", "weights_fold", "JSONB")


def downgrade() -> None:
    safe_drop_column("validator_heartbeats", "weights_fold")

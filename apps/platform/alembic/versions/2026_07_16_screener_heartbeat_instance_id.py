"""per-instance screener heartbeats

Add instance_id to screener_heartbeats and re-key it on
(screener_hotkey, instance_id). The prod screener fleet shares a single hotkey,
so without a per-instance key every worker upserts the same row and the fleet
collapses into one entry on /screeners. Existing rows (and any pre-v3 worker
that sends no instance_id) are stored under the 'legacy' sentinel.

Revision ID: b7c1e9d4a2f8
Revises: d84b3a91f620
Create Date: 2026-07-16 22:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7c1e9d4a2f8"
down_revision: str | Sequence[str] | None = "d84b3a91f620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One statement per op.execute(): asyncpg cannot prepare multiple at once.
    op.execute(
        "ALTER TABLE screener_heartbeats "
        "ADD COLUMN instance_id TEXT NOT NULL DEFAULT 'legacy'"
    )
    op.execute(
        "ALTER TABLE screener_heartbeats "
        "DROP CONSTRAINT screener_heartbeats_pkey, "
        "ADD CONSTRAINT screener_heartbeats_pkey "
        "PRIMARY KEY (screener_hotkey, instance_id)"
    )
    op.execute(
        "ALTER TABLE screener_heartbeats "
        "ADD CONSTRAINT screener_heartbeats_instance_id_length_check "
        "CHECK (length(instance_id) BETWEEN 1 AND 63)"
    )


def downgrade() -> None:
    # Collapse back to one row per hotkey: keep the most recently seen instance.
    op.execute(
        "DELETE FROM screener_heartbeats a "
        "USING screener_heartbeats b "
        "WHERE a.screener_hotkey = b.screener_hotkey "
        "AND a.seen_at < b.seen_at"
    )
    op.execute(
        "ALTER TABLE screener_heartbeats "
        "DROP CONSTRAINT screener_heartbeats_instance_id_length_check"
    )
    op.execute(
        "ALTER TABLE screener_heartbeats "
        "DROP CONSTRAINT screener_heartbeats_pkey, "
        "ADD CONSTRAINT screener_heartbeats_pkey PRIMARY KEY (screener_hotkey)"
    )
    op.execute("ALTER TABLE screener_heartbeats DROP COLUMN instance_id")

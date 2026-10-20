"""record the hardware each validator heartbeat announces

Revision ID: 3e8b1d6c9a47
Revises: 15b4db69f2ce
Create Date: 2026-10-17

The nullable JSONB column preserves every pre-v32 validator as NULL. It stores
the closed ``HostSpecs`` heartbeat model only: logical and physical CPU counts,
total RAM in MiB, total root disk in GiB, and the machine architecture -- the
same block screener heartbeats announce since protocol v6. ``validator_heartbeats``
is hot, so the metadata-only add/drop uses the bounded-lock migration helpers.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "3e8b1d6c9a47"
down_revision: str | Sequence[str] | None = "15b4db69f2ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("validator_heartbeats", "host_specs", "JSONB")


def downgrade() -> None:
    safe_drop_column("validator_heartbeats", "host_specs")

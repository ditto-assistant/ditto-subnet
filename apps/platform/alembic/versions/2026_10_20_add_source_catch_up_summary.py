"""record how the source-emission collector replays deep catch-up ranges

Revision ID: 6b2f9e4d1a37
Revises: e1b8c4f7a209
Create Date: 2026-10-20

A deep catch-up range is replayed sparsely, or falls back to the per-block
scan when its planner asks for one or cannot plan it. A fallback costs only
throughput, but it was visible nowhere. Each range now updates one nullable
JSONB summary on the collector cursor row (one row per netuid), which Backroom
shows beside the reveal-plan shadow. NULL until the first planned range.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "6b2f9e4d1a37"
down_revision: str | Sequence[str] | None = "e1b8c4f7a209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("source_emission_collector_cursors", "catch_up", "JSONB")


def downgrade() -> None:
    safe_drop_column("source_emission_collector_cursors", "catch_up")

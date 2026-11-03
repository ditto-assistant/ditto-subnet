"""record the reveal-anchored range planner's shadow comparison

Revision ID: d0250cf21e57
Revises: 3e8b1d6c9a47
Create Date: 2026-10-18

The source-emission collector shadows its event-based range planner with one
that reads only payouts and reveal-queue changes. Each comparison updates one
nullable JSONB summary on the collector cursor row (one row per netuid), so an
operator can see from Backroom whether the faster planner ever missed a block
before it is trusted. NULL until the first shadowed range.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "d0250cf21e57"
down_revision: str | Sequence[str] | None = "3e8b1d6c9a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("source_emission_collector_cursors", "reveal_plan_shadow", "JSONB")


def downgrade() -> None:
    safe_drop_column("source_emission_collector_cursors", "reveal_plan_shadow")

"""Record the review budget stop and lineage diff size per screening attempt.

``screening_review_usage.budget_stop`` keeps the worker's ReviewBudgetStop
(which limit ended an L2/L3 trajectory, the limits applied and the aggregate
usage at the stop). ``screening_attempts.lineage_changed_file_count`` and
``lineage_changed_line_count`` keep the bound lineage packet's diff size,
which was otherwise only visible by rebuilding the packet from storage.

All three are nullable with no default and no backfill: rows written before
this revision keep NULL. ``screening_attempts`` is hot, so each column is
added on its own through ``safe_add_column``.

Revision ID: 3f7a1c9e5b26
Revises: 4c8e2a6f1d93
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "3f7a1c9e5b26"
down_revision: str | Sequence[str] | None = "4c8e2a6f1d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    safe_add_column("screening_review_usage", "budget_stop", "JSONB")
    safe_add_column("screening_attempts", "lineage_changed_file_count", "INTEGER")
    safe_add_column("screening_attempts", "lineage_changed_line_count", "INTEGER")


def downgrade() -> None:
    safe_drop_column("screening_attempts", "lineage_changed_line_count")
    safe_drop_column("screening_attempts", "lineage_changed_file_count")
    safe_drop_column("screening_review_usage", "budget_stop")

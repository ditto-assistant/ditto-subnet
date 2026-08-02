"""raise the public source-release embargo ceiling to 720h (30 days)

Revision ID: c8a2f640d31e
Revises: e8c31f4a76d2
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c8a2f640d31e"
down_revision: str | None = "e8c31f4a76d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "artifact_release_settings_revisions"
_CHECK = "artifact_release_settings_embargo_hours_check"


def upgrade() -> None:
    # Range bound only. The operative 48-hour window is untouched: this widens
    # what an operator may choose, it does not choose anything. No revision is
    # appended, so the effective embargo after this migration is whatever the
    # current head already says.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 720")


def downgrade() -> None:
    # Rows above the restored bound must be reconciled before the tighter
    # constraint can be re-added. Clamp rather than delete: `parent_revision`
    # is a unique chain pointer, so deleting a revision that another row
    # already points at leaves an orphaned chain the API's CAS check reads as
    # permanently stale. Clamping rewrites the value and keeps the chain whole.
    #
    # This *shortens* the effective embargo if the current head is above 48,
    # which releases king source earlier and cannot be undone. That is inherent
    # to restoring the old ceiling, not something the clamp introduces.
    op.execute(f"UPDATE {_TABLE} SET embargo_hours = 48 WHERE embargo_hours > 48")
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 48")

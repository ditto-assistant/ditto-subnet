"""let the release policy say "never", and raise the finite ceiling to a year

Revision ID: f4b7d2c91ae5
Revises: b2e9d4a17c60
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "f4b7d2c91ae5"
down_revision: str | None = "b2e9d4a17c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "artifact_release_settings_revisions"
_HOURS_CHECK = "artifact_release_settings_embargo_hours_check"
_DISCLOSURE_CHECK = "artifact_release_settings_disclosure_check"


def upgrade() -> None:
    # Range and vocabulary only. No revision is appended, so the effective
    # policy after this migration is whatever the current head already says --
    # today a 48-hour public window. This widens what an operator may choose;
    # it does not choose anything.
    #
    # `safe_add_column` rather than `op.add_column` even though this table is
    # tiny and append-only: every public response reads it through
    # `artifact_release_policy`, and `ADD COLUMN` takes an AccessExclusiveLock
    # that blocks readers as well as writers. The one-shot branch applies (no
    # backfill, NOT NULL, non-volatile default), so PostgreSQL 11+ stores the
    # default in `pg_attribute.attmissingval` and never rewrites the heap.
    safe_add_column(
        _TABLE,
        "disclosure",
        "TEXT",
        not_null=True,
        server_default="'public'",
    )
    op.create_check_constraint(
        _DISCLOSURE_CHECK, _TABLE, "disclosure IN ('public', 'never')"
    )

    op.drop_constraint(_HOURS_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_HOURS_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 8760")


def downgrade() -> None:
    # Dropping `disclosure` makes a `never` policy public again the moment the
    # old build serves the next request. That is inherent to reverting the
    # feature, and it is the reason a downgrade here is an incident rather than
    # a rollback -- there is no ordering that avoids it, because the old build
    # has no vocabulary for withholding.
    op.drop_constraint(_DISCLOSURE_CHECK, _TABLE, type_="check")
    safe_drop_column(_TABLE, "disclosure")

    # Rows above the restored bound must be reconciled before the tighter
    # constraint can be re-added. Clamp rather than delete, exactly as
    # c8a2f640d31e does: `parent_revision` is a unique chain pointer, so
    # deleting a revision another row points at leaves an orphaned chain the
    # API's CAS check reads as permanently stale.
    #
    # This *shortens* the effective window if the current head is above 720,
    # which releases king source earlier and cannot be undone.
    op.execute(f"UPDATE {_TABLE} SET embargo_hours = 720 WHERE embargo_hours > 720")
    op.drop_constraint(_HOURS_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_HOURS_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 720")

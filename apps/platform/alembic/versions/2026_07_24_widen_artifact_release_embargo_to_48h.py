"""widen public source-release embargo to 48h and adopt it as the default

Revision ID: b3f9a1c72e40
Revises: d5f1a8c62b93
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b3f9a1c72e40"
down_revision: str | None = "d5f1a8c62b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "artifact_release_settings_revisions"
_CHECK = "artifact_release_settings_embargo_hours_check"


def upgrade() -> None:
    # Raise the ceiling from 24h to 48h. The window may now be lengthened as
    # well as shortened, so the constraint only bounds the range.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 48")

    # Adopt the community-agreed 48-hour window as the operative default by
    # appending a revision chained to the current head. Idempotent-safe: only
    # inserts when the current embargo is not already 48.
    op.execute(
        """
        INSERT INTO artifact_release_settings_revisions
            (parent_revision, embargo_hours, reason, actor)
        SELECT
            COALESCE(MAX(revision), 0),
            48,
            'Adopt the 48-hour public source-release window (SN118 community decision)',
            'migration'
        FROM artifact_release_settings_revisions
        HAVING COALESCE(
            (
                SELECT embargo_hours
                FROM artifact_release_settings_revisions
                ORDER BY revision DESC
                LIMIT 1
            ),
            0
        ) <> 48
        """
    )


def downgrade() -> None:
    # Restore the 24-hour ceiling. Any rows above the old bound must go first,
    # or re-adding the tighter constraint would fail.
    op.execute(
        "DELETE FROM artifact_release_settings_revisions WHERE embargo_hours > 24"
    )
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(_CHECK, _TABLE, "embargo_hours BETWEEN 6 AND 24")

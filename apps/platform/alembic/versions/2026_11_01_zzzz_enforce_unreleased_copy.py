"""switch the unreleased-copy signal to enforce (operator-approved)

Revision ID: 9c4b2e7f1a36
Revises: 7a3e5c1d9f02
Create Date: 2026-11-01

Appends the first audited revision of ``unreleased_copy_settings_revisions``:
mode ``enforce``, threshold 100 (recalibrated at full scale: the smallest
ruled copy scores 113.75 and the highest-scoring file-reviewed negative
84.25, so no listed negative flags). Approved by
the subnet owner on 2026-11-01. Enforce only opens copy holds for an operator
ruling on agents uploaded after this revision; it never rejects, and earlier
flags stay on the review list.

Data only and idempotent: nothing is written when any revision already
exists, so an operator's own revision always wins. Reverting is an ordinary
Backroom ``apply_unreleased_copy_settings`` write (mode shadow or off).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c4b2e7f1a36"
down_revision: str | Sequence[str] | None = "7a3e5c1d9f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO unreleased_copy_settings_revisions "
            "(parent_revision, mode, threshold, reason, actor) "
            "SELECT 0, 'enforce', 100, 'Peyton approved 2026-11-01', "
            "'migration (subnet owner approval relayed by coordinator)' "
            "WHERE NOT EXISTS (SELECT 1 FROM unreleased_copy_settings_revisions)"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM unreleased_copy_settings_revisions "
            "WHERE revision = 1 AND reason = 'Peyton approved 2026-11-01' "
            "AND actor = 'migration (subnet owner approval relayed by coordinator)'"
        )
    )

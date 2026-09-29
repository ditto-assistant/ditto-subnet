"""Bind report-only L2 canaries to an isolated review-settings revision.

A canary could previously run only under the claiming node's effective review
settings, so an experiment posture had to be written to a node or worker scope
that production screening on that node also resolves. A canary now records an
immutable ``l2-report-canary*`` revision at scheduling time and runs under it.

The CHECK keeps the three pin columns all-or-nothing and refuses any scope
outside the canary namespace, so a pin can never name ``*``, ``bootstrap``, a
node, or a worker scope even if application validation regresses.

Revision ID: 111add4c7a2a
Revises: 5e2a8c4f9d17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "111add4c7a2a"
down_revision: str | Sequence[str] | None = "5e2a8c4f9d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "screener_l2_report_canaries"
_FKEY = "screener_l2_canary_review_settings_revision_fkey"
_CHECK = "ck_screener_l2_report_canaries_review_settings_pin_check"


def upgrade() -> None:
    # A small operator-only table: nullable columns without defaults are
    # metadata-only, and every existing row satisfies the CHECK as all-NULL.
    op.add_column(_TABLE, sa.Column("review_settings_revision", sa.Integer()))
    op.add_column(_TABLE, sa.Column("review_settings_scope", sa.Text()))
    op.add_column(_TABLE, sa.Column("review_settings_checksum", sa.Text()))
    op.create_foreign_key(
        _FKEY,
        _TABLE,
        "screener_review_settings_revisions",
        ["review_settings_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f(_CHECK),
        _TABLE,
        "(review_settings_revision IS NULL "
        "AND review_settings_scope IS NULL "
        "AND review_settings_checksum IS NULL) OR "
        "(review_settings_revision IS NOT NULL "
        "AND review_settings_scope IS NOT NULL "
        "AND review_settings_checksum IS NOT NULL "
        "AND review_settings_revision > 0 "
        "AND (review_settings_scope = 'l2-report-canary' "
        "OR review_settings_scope LIKE 'l2-report-canary-%') "
        "AND review_settings_checksum ~ '^[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.drop_constraint(_FKEY, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "review_settings_checksum")
    op.drop_column(_TABLE, "review_settings_scope")
    op.drop_column(_TABLE, "review_settings_revision")

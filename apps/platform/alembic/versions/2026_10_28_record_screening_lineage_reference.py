"""Record why a lineage packet went full and which reference it diffed against.

``lineage_full_review_reasons`` (JSON list of reason strings, empty for a diff
scope) and ``lineage_reference_kind`` (same_owner_version | same_owner |
released_base) are written when the worker's packet binds, beside the existing
scope and digest. Both stay NULL until a packet is bound; rows bound before
this migration keep NULL.

Revision ID: 5b8e2d4f6a13
Revises: 7c0f3e1a9b42
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5b8e2d4f6a13"
down_revision = "7c0f3e1a9b42"
branch_labels = None
depends_on = None

_TABLE = "screening_attempts"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("lineage_full_review_reasons", postgresql.JSONB(), nullable=True),
    )
    op.add_column(_TABLE, sa.Column("lineage_reference_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        "screening_attempts_lineage_record_check",
        _TABLE,
        "lineage_scope IS NOT NULL OR (lineage_full_review_reasons IS NULL "
        "AND lineage_reference_kind IS NULL)",
    )
    op.create_check_constraint(
        "screening_attempts_lineage_reference_kind_check",
        _TABLE,
        "lineage_reference_kind IS NULL OR lineage_reference_kind IN "
        "('same_owner_version', 'same_owner', 'released_base')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_attempts_lineage_reference_kind_check", _TABLE, type_="check"
    )
    op.drop_constraint("screening_attempts_lineage_record_check", _TABLE, type_="check")
    op.drop_column(_TABLE, "lineage_reference_kind")
    op.drop_column(_TABLE, "lineage_full_review_reasons")

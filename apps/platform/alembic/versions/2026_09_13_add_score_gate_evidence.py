"""Bench v13 gate evidence on scores and gate-note links on disputes.

Revision ID: 9e4b7c2d1a63
Revises: 9e4b2f7c1a53
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "9e4b7c2d1a63"
down_revision = "9e4b2f7c1a53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The typed per-case gate notes + run-level shadow verdict the platform
    # projects from a v13+ score report. Nullable and additive: every existing
    # row stays byte-identical, and a v13+ report from a scorer that emitted no
    # gate telemetry stores NULL. The floor is a CHECK so no application bug can
    # attach gate evidence to a pre-v13 era; it is a floor (>= 13), never an
    # equality, so later versions carry it without another migration.
    op.add_column(
        "scores",
        sa.Column("gate_evidence", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "scores_gate_evidence_bench_floor",
        "scores",
        "gate_evidence IS NULL OR bench_version >= 13",
    )
    # A dispute may cite the exact gate notes it contests. Ids re-derive from
    # the submission's own scores, so the column holds references, never
    # verdict content.
    op.add_column(
        "screening_disputes",
        sa.Column("gate_note_ids", postgresql.JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("screening_disputes", "gate_note_ids")
    op.drop_constraint("scores_gate_evidence_bench_floor", "scores", type_="check")
    op.drop_column("scores", "gate_evidence")

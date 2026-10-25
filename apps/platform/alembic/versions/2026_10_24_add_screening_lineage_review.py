"""Bind a screening attempt to its lineage-scoped review packet.

A same-owner resubmission is reviewed against its prior version: the attempt
records which prior agent supplied the diff base and history, the scope the
packet selected (``diff`` or ``full``), and the digest of the exact packet the
worker received. All three are NULL for an ordinary full review.

Revision ID: b7e4c2a9d1f3
Revises: 3d7a9c1e5b24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e4c2a9d1f3"
down_revision: str | Sequence[str] | None = "3d7a9c1e5b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_attempts",
        sa.Column("lineage_parent_agent_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "screening_attempts",
        sa.Column("lineage_scope", sa.Text(), nullable=True),
    )
    op.add_column(
        "screening_attempts",
        sa.Column("lineage_packet_sha256", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "screening_attempts_lineage_parent_fkey",
        "screening_attempts",
        "agents",
        ["lineage_parent_agent_id"],
        ["agent_id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "screening_attempts_lineage_binding_check",
        "screening_attempts",
        "(lineage_scope IS NULL AND lineage_packet_sha256 IS NULL) OR "
        "(lineage_parent_agent_id IS NOT NULL "
        "AND lineage_scope IN ('diff', 'full') "
        "AND length(lineage_packet_sha256) = 64)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_attempts_lineage_binding_check",
        "screening_attempts",
        type_="check",
    )
    op.drop_constraint(
        "screening_attempts_lineage_parent_fkey",
        "screening_attempts",
        type_="foreignkey",
    )
    op.drop_column("screening_attempts", "lineage_packet_sha256")
    op.drop_column("screening_attempts", "lineage_scope")
    op.drop_column("screening_attempts", "lineage_parent_agent_id")

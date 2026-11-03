"""add the distinctive-item index and shadow unreleased-copy signals

Revision ID: 5d1c7e9a2b40
Revises: 8c41e2f7a9d3
Create Date: 2026-11-01

Two new tables, nothing on a hot table. ``agent_distinctive_items`` holds one
sorted ``bigint[]`` of language-agnostic item keys per artifact (about 8 bytes
per item, no paths or text); ``unreleased_copy_signals`` records each shadow
evaluation with its operator-only evidence.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5d1c7e9a2b40"
down_revision: str | Sequence[str] | None = "8c41e2f7a9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_distinctive_items",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("items_version", sa.Text(), nullable=False),
        sa.Column("item_keys", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "indexed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "unreleased_copy_signals",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("signal_version", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("flagged", sa.Boolean(), nullable=False),
        sa.Column("top_score", sa.Float(), nullable=False),
        sa.Column("top_matched_agent_id", sa.UUID(), nullable=True),
        sa.Column("compared_agent_count", sa.Integer(), nullable=False),
        sa.Column("candidate_item_count", sa.Integer(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('shadow', 'enforce')", name="unreleased_copy_signals_mode_check"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "signal_version"),
    )
    op.create_index(
        "unreleased_copy_signals_flagged_idx",
        "unreleased_copy_signals",
        ["signal_version", "flagged", "top_score"],
    )


def downgrade() -> None:
    op.drop_index(
        "unreleased_copy_signals_flagged_idx", table_name="unreleased_copy_signals"
    )
    op.drop_table("unreleased_copy_signals")
    op.drop_table("agent_distinctive_items")

"""add agent_fork_bases for the fork-must-beat-base emission gate

Revision ID: 9f3a6c1e5b27
Revises: 3f6a8d2c9e14
Create Date: 2026-11-01

One new table, nothing on a hot table. Each row records which publicly
released other-owner agent an artifact forks (or none), written by the
unreleased-copy sentinel and read by the emission eligibility gate.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f3a6c1e5b27"
down_revision: str | Sequence[str] | None = "3f6a8d2c9e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_fork_bases",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("detector_version", sa.Text(), nullable=False),
        sa.Column("base_agent_id", sa.UUID(), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("shared_count", sa.Integer(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["base_agent_id"], ["agents.agent_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("agent_id", "detector_version"),
    )
    op.create_index("agent_fork_bases_base_idx", "agent_fork_bases", ["base_agent_id"])


def downgrade() -> None:
    op.drop_index("agent_fork_bases_base_idx", table_name="agent_fork_bases")
    op.drop_table("agent_fork_bases")

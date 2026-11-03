"""add competition improvement metrics tables

Revision ID: 3f6a8d2c9e14
Revises: b4f81d2c6e93
Create Date: 2026-11-01

Measurement only. Three new tables, nothing on a hot table:
``competition_improvement_daily`` is one row per bench version and UTC day
(frontier, champion, owner-deduplicated top-N, new highs, releases and forks
to date); ``competition_source_releases`` and ``competition_release_forks``
hold each exact-SHA public handoff and the later other-owner uploads detected
as forks of it, with lag, size of change and score delta.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3f6a8d2c9e14"
down_revision: str | Sequence[str] | None = "b4f81d2c6e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _computed_at() -> sa.Column:
    return sa.Column(
        "computed_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "competition_improvement_daily",
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("metrics_version", sa.Text(), nullable=False),
        sa.Column("frontier_composite", sa.Float(), nullable=True),
        sa.Column("frontier_agent_id", sa.UUID(), nullable=True),
        sa.Column("champion_agent_id", sa.UUID(), nullable=True),
        sa.Column("champion_composite", sa.Float(), nullable=True),
        sa.Column("top_n_mean", sa.Float(), nullable=True),
        sa.Column("top_n_min", sa.Float(), nullable=True),
        sa.Column("finalized_agent_count", sa.Integer(), nullable=False),
        sa.Column("new_high_count", sa.Integer(), nullable=False),
        sa.Column("releases_on_day", sa.Integer(), nullable=False),
        sa.Column("releases_to_date", sa.Integer(), nullable=False),
        sa.Column("forks_on_day", sa.Integer(), nullable=False),
        sa.Column("forks_to_date", sa.Integer(), nullable=False),
        sa.Column("thin_forks_to_date", sa.Integer(), nullable=False),
        sa.Column("embargo_hours", sa.Integer(), nullable=False),
        sa.Column("disclosure", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("backfilled", sa.Boolean(), nullable=False),
        _computed_at(),
        sa.PrimaryKeyConstraint("bench_version", "day"),
    )
    op.create_table(
        "competition_source_releases",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("metrics_version", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=True),
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("embargo_hours", sa.Integer(), nullable=False),
        sa.Column("disclosure", sa.Text(), nullable=False),
        sa.Column("base_composite", sa.Float(), nullable=True),
        _computed_at(),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "competition_release_forks",
        sa.Column("base_agent_id", sa.UUID(), nullable=False),
        sa.Column("fork_agent_id", sa.UUID(), nullable=False),
        sa.Column("metrics_version", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=True),
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("fork_uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lag_hours", sa.Float(), nullable=False),
        sa.Column("containment", sa.Float(), nullable=True),
        sa.Column("novel_fraction", sa.Float(), nullable=True),
        sa.Column("declared", sa.Boolean(), nullable=False),
        sa.Column("thin", sa.Boolean(), nullable=False),
        sa.Column("base_composite", sa.Float(), nullable=True),
        sa.Column("fork_composite", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("shared_seed_count", sa.Integer(), nullable=False),
        sa.Column("shared_seed_delta", sa.Float(), nullable=True),
        _computed_at(),
        sa.ForeignKeyConstraint(
            ["base_agent_id"], ["agents.agent_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["fork_agent_id"], ["agents.agent_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("base_agent_id", "fork_agent_id"),
    )
    op.create_index(
        "competition_release_forks_fork_idx",
        "competition_release_forks",
        ["fork_agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "competition_release_forks_fork_idx", table_name="competition_release_forks"
    )
    op.drop_table("competition_release_forks")
    op.drop_table("competition_source_releases")
    op.drop_table("competition_improvement_daily")

"""add generator source releases (bench v16, dueling)

Revision ID: 8e2a4c6d1f37
Revises: 3c8f1d2a9b64
Create Date: 2026-11-03

Every generator that held a column in a dueling season has its exact pinned
archive published once ``release_at = max(reveal of the last season that
pinned it, close of the first season that pinned it + embargo_hours)``
(``dueling-bench.md`` section 2, "Delayed reproducibility"). One row per
pinned submission; a new, empty table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8e2a4c6d1f37"
down_revision: str | Sequence[str] | None = "3c8f1d2a9b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "generator_source_releases"


def upgrade() -> None:
    ts = sa.TIMESTAMP(timezone=True)
    op.create_table(
        TABLE,
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("tree_sha256", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("first_season_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_season_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seasons", sa.Integer(), nullable=False),
        sa.Column("first_closed_at", ts),
        sa.Column("last_revealed_at", ts),
        sa.Column("embargo_hours", sa.Integer(), nullable=False),
        sa.Column("disclosure", sa.Text(), nullable=False),
        sa.Column("release_at", ts),
        sa.Column("released_at", ts),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("updated_at", ts, nullable=False),
        sa.Column("created_at", ts, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["generator_submissions.submission_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "bench_version >= 16", name="generator_source_releases_bench_floor"
        ),
        sa.CheckConstraint(
            "seasons >= 1 AND embargo_hours >= 0",
            name="generator_source_releases_counts_check",
        ),
        sa.CheckConstraint(
            "released_at IS NULL OR release_at IS NOT NULL",
            name="generator_source_releases_released_check",
        ),
    )
    op.create_index(
        "generator_source_releases_release_idx", TABLE, ["released_at", "release_at"]
    )


def downgrade() -> None:
    op.drop_index("generator_source_releases_release_idx", table_name=TABLE)
    op.drop_table(TABLE)

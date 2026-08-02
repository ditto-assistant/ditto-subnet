"""Cover historical benchmark memory timeline reads.

Revision ID: b5d3f8a20c71
Revises: c4d7e8f9a0b1
Create Date: 2026-07-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b5d3f8a20c71"
down_revision: str | Sequence[str] | None = "c4d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "scores_bench_timeline_idx",
        "scores",
        ["bench_version", "agent_id", "updated_at", "validator_hotkey"],
        postgresql_include=["memory_mean", "composite", "n"],
    )


def downgrade() -> None:
    op.drop_index("scores_bench_timeline_idx", table_name="scores")

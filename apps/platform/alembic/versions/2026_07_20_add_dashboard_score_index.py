"""Index benchmark-scoped dashboard score reads.

Revision ID: f8c2d6a04e71
Revises: b7f2c8d41a95
Create Date: 2026-07-20 16:30:00.000000

The overview ledger and operations snapshot both select the current benchmark
version before ranking or aggregating scores.  The primary key starts with
``agent_id``, so those global version reads otherwise walk historical benchmark
eras as the ledger grows.  ``updated_at`` covers the operations snapshot's
last-score aggregate without widening the ordered key.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f8c2d6a04e71"
down_revision: str | Sequence[str] | None = "b7f2c8d41a95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "scores_bench_version_agent_composite_idx",
        "scores",
        ["bench_version", "agent_id", "composite", "validator_hotkey"],
        postgresql_include=["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "scores_bench_version_agent_composite_idx",
        table_name="scores",
    )

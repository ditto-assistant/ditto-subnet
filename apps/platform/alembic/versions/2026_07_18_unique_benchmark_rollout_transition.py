"""Prevent duplicate benchmark-version transitions.

Revision ID: e9b7c4a12d63
Revises: b8e4a91c2f30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e9b7c4a12d63"
down_revision: str | Sequence[str] | None = "b8e4a91c2f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "benchmark_rollouts_transition_idx",
        "benchmark_rollouts",
        ["from_version", "desired_version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("benchmark_rollouts_transition_idx", table_name="benchmark_rollouts")

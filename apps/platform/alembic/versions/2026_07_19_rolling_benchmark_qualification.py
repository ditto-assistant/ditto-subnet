"""allow rolling benchmark qualification beyond the initial top five

Revision ID: a6c3d291fbe4
Revises: e9b7c4a12d63
Create Date: 2026-07-19 02:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a6c3d291fbe4"
down_revision: str | Sequence[str] | None = "e9b7c4a12d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("benchmark_rollout_members") as batch:
        batch.drop_constraint("benchmark_member_position", type_="check")
        batch.create_check_constraint("benchmark_member_position", "position > 0")


def downgrade() -> None:
    # The restored constraint intentionally makes downgrade fail rather than
    # discard qualified members if a rollout has already expanded past five.
    with op.batch_alter_table("benchmark_rollout_members") as batch:
        batch.drop_constraint("benchmark_member_position", type_="check")
        batch.create_check_constraint(
            "benchmark_member_position", "position BETWEEN 1 AND 5"
        )

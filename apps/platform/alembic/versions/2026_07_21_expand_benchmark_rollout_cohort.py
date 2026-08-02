"""expand benchmark rollout cohort to the inherited top 25

Revision ID: f3a7c91d2e04
Revises: d2e4f6a80b13
Create Date: 2026-07-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f3a7c91d2e04"
down_revision: str | None = "d2e4f6a80b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "benchmark_rollout_five_members",
        "benchmark_rollouts",
        type_="check",
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_members",
        "benchmark_rollouts",
        "cohort_size BETWEEN 5 AND 25",
    )


def downgrade() -> None:
    # A 6-25 member rollout cannot be represented by the old schema. Refuse a
    # lossy downgrade instead of silently discarding frozen members or scores.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM benchmark_rollouts WHERE cohort_size <> 5
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade while a benchmark rollout has more than five members';
          END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "benchmark_rollout_bounded_members",
        "benchmark_rollouts",
        type_="check",
    )
    op.create_check_constraint(
        "benchmark_rollout_five_members",
        "benchmark_rollouts",
        "cohort_size = 5",
    )

"""allow generator runs of up to 80 cases per seed

Revision ID: 9d1b6a3f5c28
Revises: 4f8c2d6a9e17
Create Date: 2026-10-26

The generator competition's ``cases_per_seed`` cap rises from 32 to 80 (the
pack contract's ``challenge.MaxCases``), so a qualification run can carry up
to 80 cases per seed. ``generator_runs`` bounded the column at 8..64; this
widens it to 8..80. ``generator_runs`` is not a hot table and every existing
row already satisfies the wider bound, so the swap is a plain drop and add.
Re-runnable: the drop tolerates a constraint that is already gone.

Downgrade restores 8..64 and refuses while a run above 64 exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d1b6a3f5c28"
down_revision: str | Sequence[str] | None = "4f8c2d6a9e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "generator_runs"
_NAME = "ck_generator_runs_generator_runs_cases_check"


def _swap(bound: int) -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_NAME}")
    op.create_check_constraint(
        op.f(_NAME), _TABLE, f"cases_per_seed BETWEEN 8 AND {bound}"
    )


def upgrade() -> None:
    _swap(80)


def downgrade() -> None:
    wide = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE cases_per_seed > 64")
    )
    if wide:
        raise RuntimeError(
            f"Cannot downgrade: {wide} generator run(s) carry more than 64 cases "
            "per seed, which the older bound rejects."
        )
    _swap(64)

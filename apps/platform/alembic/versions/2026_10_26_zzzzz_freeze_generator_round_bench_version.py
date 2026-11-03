"""freeze each generator round's bench version when it opens

Revision ID: 3e8a5c1f7b94
Revises: 5b7e1c9d3a42
Create Date: 2026-10-26

A generator round now records the bench contract it runs at, frozen when it
opens (like ``cases_per_column``), so a benchmark activation mid-round never
moves the round's panel runs, tickets or capability routing. ``NULL`` means
the round has not opened, or was voided at opening before a contract was
chosen.

Every round opened before this column existed ran at bench 13, the only
version the round lane has ever issued, so those rows are backfilled to 13.
``generator_rounds`` is not a hot table (one row per round), so this is a
plain add, backfill and check. Re-runnable: the add and the check tolerate an
earlier partial run.

Downgrade drops the column; the lane then falls back to its legacy v13 pin.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3e8a5c1f7b94"
down_revision: str | Sequence[str] | None = "5b7e1c9d3a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "generator_rounds"
_CHECK = "ck_generator_rounds_generator_rounds_bench_version_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS bench_version integer")
    op.execute(
        f"UPDATE {_TABLE} SET bench_version = 13 "
        "WHERE bench_version IS NULL AND seed IS NOT NULL"
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.create_check_constraint(
        op.f(_CHECK),
        _TABLE,
        "bench_version IS NULL OR bench_version >= 13",
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS bench_version")

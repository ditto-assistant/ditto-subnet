"""allow a benchmark rollout to be superseded without activating

Revision ID: b7f2c8d41a95
Revises: c5b9e2a7d410
Create Date: 2026-07-19 12:00:00.000000

``superseded`` is a terminal status for a rollout that an operator abandoned
before activation. ``benchmark_rollouts_one_open_idx`` is partial on
``status IN ('collecting', 'blocked_ineligible')``, so a superseded row leaves
the single open slot immediately -- no index change is required here, and the
next rollout inserts cleanly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7f2c8d41a95"
down_revision: str | Sequence[str] | None = "c5b9e2a7d410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDENED = "status IN ('collecting', 'blocked_ineligible', 'activated', 'superseded')"
_ORIGINAL = "status IN ('collecting', 'blocked_ineligible', 'activated')"


def upgrade() -> None:
    with op.batch_alter_table("benchmark_rollouts") as batch:
        batch.drop_constraint("benchmark_rollout_status", type_="check")
        batch.create_check_constraint("benchmark_rollout_status", _WIDENED)


def downgrade() -> None:
    # The narrowed constraint intentionally makes downgrade fail rather than
    # silently rewrite an abandoned rollout back into an open or activated one.
    with op.batch_alter_table("benchmark_rollouts") as batch:
        batch.drop_constraint("benchmark_rollout_status", type_="check")
        batch.create_check_constraint("benchmark_rollout_status", _ORIGINAL)

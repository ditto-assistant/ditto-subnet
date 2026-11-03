"""let validator_tickets.attempt_count be zero for round-only rows

Revision ID: 3d7a9c1e5b24
Revises: 8b3d6f0c2a71
Create Date: 2026-10-24

``attempt_count`` counts scoring leases, and the canonical retry budget is
``attempt_count >= 1 + manual_retry_grants``. A generator-round lease is not a
scoring attempt, but when it created the ``(agent, 13, validator)`` row the
``attempt_count > 0`` CHECK forced it to record one, so that round run spent the
validator's single canonical attempt on the agent (SN-334). A round-created row
now starts at zero: no scoring lease yet.

Relaxing ``> 0`` to ``>= 0`` admits every existing row, so this is a
constraint swap with no backfill. ``validator_tickets`` is a hot table: the
swap is one ``ALTER TABLE`` that adds the new CHECK ``NOT VALID`` (metadata
only, one short ``ACCESS EXCLUSIVE`` window), and the scan that marks it valid
runs afterwards in its own transaction under ``SHARE UPDATE EXCLUSIVE``, which
does not block ticket writers. Re-runnable from any point.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3d7a9c1e5b24"
down_revision: str | Sequence[str] | None = "8b3d6f0c2a71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "validator_tickets_attempt_count_positive"
_NEW = "validator_tickets_attempt_count_nonnegative"


def _constraint_exists(name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = :name "
                "AND conrelid = 'validator_tickets'::regclass"
            ),
            {"name": name},
        )
    )


def upgrade() -> None:
    if not _constraint_exists(_NEW):
        op.execute(
            f"ALTER TABLE validator_tickets "
            f"DROP CONSTRAINT IF EXISTS {_OLD}, "
            f"ADD CONSTRAINT {_NEW} CHECK (attempt_count >= 0) NOT VALID"
        )
    else:
        op.execute(f"ALTER TABLE validator_tickets DROP CONSTRAINT IF EXISTS {_OLD}")
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE validator_tickets VALIDATE CONSTRAINT {_NEW}")


def downgrade() -> None:
    # A round-only row has had no scoring lease. The old schema cannot say
    # that, so it records the one lease the round took, as it did before.
    op.execute("UPDATE validator_tickets SET attempt_count = 1 WHERE attempt_count = 0")
    op.execute(
        f"ALTER TABLE validator_tickets "
        f"DROP CONSTRAINT IF EXISTS {_NEW}, "
        f"ADD CONSTRAINT {_OLD} CHECK (attempt_count > 0)"
    )

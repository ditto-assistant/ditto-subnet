"""Allow the $1 conversation hold alongside the legacy $30 one.

Revision ID: b7e3c1d9a420
Revises: 4c1e7a9d2b58
Create Date: 2026-11-08

The check pinned every row to the $30 v1 hold. The resize (Peyton,
2026-11-08) admits new assessments at $1 ($0.85 judge + $0.15 harness).
Existing $30 rows remain valid and are not rewritten.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e3c1d9a420"
down_revision: str | None = "4c1e7a9d2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_conversation_assessments_conversation_reserved_cost"


def upgrade() -> None:
    op.execute(f"ALTER TABLE conversation_assessments DROP CONSTRAINT {_CHECK}")
    op.execute(
        f"ALTER TABLE conversation_assessments ADD CONSTRAINT {_CHECK} "
        "CHECK (reserved_microusd IN (30000000, 1000000))"
    )


def downgrade() -> None:
    # Fails while any $1 row exists: a downgrade must not rewrite real holds.
    op.execute(f"ALTER TABLE conversation_assessments DROP CONSTRAINT {_CHECK}")
    op.execute(
        f"ALTER TABLE conversation_assessments ADD CONSTRAINT {_CHECK} "
        "CHECK (reserved_microusd = 30000000)"
    )

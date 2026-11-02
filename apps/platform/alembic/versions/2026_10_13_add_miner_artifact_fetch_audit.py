"""Allow authenticated miner owners in the artifact fetch audit.

Revision ID: a4963e2b18c7
Revises: 7d3ca1e6a290
Create Date: 2026-10-13
"""

from alembic import op

revision: str = "a4963e2b18c7"
down_revision: str | None = "7d3ca1e6a290"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "artifact_fetch_audit"
CONSTRAINT = "artifact_fetch_audit_requester_kind_check"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "requester_kind IN ('validator', 'screener', 'admin', 'public', 'miner')",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    # Retain historical miner audit rows, while restoring the old vocabulary
    # for new writes. Deleting or relabelling the audit would erase identity.
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "requester_kind IN ('validator', 'screener', 'admin', 'public')",
        postgresql_not_valid=True,
    )

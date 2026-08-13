"""Index bounded public activity pagination.

Revision ID: c6e2f93a8b10
Revises: e8a4c91d7f20
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c6e2f93a8b10"
down_revision: str | Sequence[str] | None = "e8a4c91d7f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "agents_created_agent_idx",
        "agents",
        ["created_at", "agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("agents_created_agent_idx", table_name="agents")

"""merge inference concurrency and lease audit heads

Revision ID: dbc8ebf20a23
Revises: e7b4c02a5d18, e5b8c31d47af
Create Date: 2026-07-25 12:29:16.175743

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "dbc8ebf20a23"
down_revision: str | Sequence[str] | None = ("e7b4c02a5d18", "e5b8c31d47af")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

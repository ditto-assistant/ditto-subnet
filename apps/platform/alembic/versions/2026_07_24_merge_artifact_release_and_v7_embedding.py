"""merge artifact release settings and v7 embedding accounting

Revision ID: c6e9a1d47b20
Revises: a7c4e2f913bd, f4a8c21e7d60
Create Date: 2026-07-24
"""

from collections.abc import Sequence

revision: str = "c6e9a1d47b20"
down_revision: tuple[str, str] = ("a7c4e2f913bd", "f4a8c21e7d60")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two already-applied metadata branches."""


def downgrade() -> None:
    """Split back to the two parent heads without changing data."""

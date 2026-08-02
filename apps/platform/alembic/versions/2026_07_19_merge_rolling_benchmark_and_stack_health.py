"""merge rolling benchmark and validator stack health heads

Revision ID: c5b9e2a7d410
Revises: a6c3d291fbe4, a1d4f8b6c9e2
Create Date: 2026-07-19 03:05:00.000000
"""

from collections.abc import Sequence

revision: str = "c5b9e2a7d410"
down_revision: str | Sequence[str] | None = (
    "a6c3d291fbe4",
    "a1d4f8b6c9e2",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

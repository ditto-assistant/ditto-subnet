"""merge screened-image and validator-retry migration heads

Revision ID: f3a7c9d2e4b1
Revises: d2e5f7a9c1b3, e4a2b9c71d60
Create Date: 2026-07-18 16:30:00.000000
"""

from collections.abc import Sequence

revision: str = "f3a7c9d2e4b1"
down_revision: str | Sequence[str] | None = (
    "d2e5f7a9c1b3",
    "e4a2b9c71d60",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

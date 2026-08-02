"""Merge payment-rate and screener-review migration heads.

Revision ID: a8d4c6e21f90
Revises: c7d2a10f4e9b, e3f5a7b91c24
Create Date: 2026-07-22
"""

from collections.abc import Sequence

revision: str = "a8d4c6e21f90"
down_revision: str | Sequence[str] | None = ("c7d2a10f4e9b", "e3f5a7b91c24")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

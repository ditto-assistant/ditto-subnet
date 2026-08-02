"""add public-safe screening reason to agents

Revision ID: f6c2d0e35a41
Revises: e5b1c9d24f30
Create Date: 2026-07-14 00:40:00.000000

Persists a short, public-safe failure category for submissions that fail the
build/serve gate. The raw screener detail may contain an untrusted Docker build
log and remains transient; it is never logged or stored.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c2d0e35a41"
down_revision: str | Sequence[str] | None = "e5b1c9d24f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable public-safe screening reason."""
    op.execute("ALTER TABLE agents ADD COLUMN screening_reason TEXT")


def downgrade() -> None:
    """Remove the public-safe screening reason."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS screening_reason")

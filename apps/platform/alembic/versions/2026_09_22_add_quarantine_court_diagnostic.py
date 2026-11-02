"""Persist a sanitized automated-court failure trace on quarantine rows.

Revision ID: c91e4b7a2d08
Revises: e804a171db92
Create Date: 2026-09-22

Held source reviews that stop inside the adjudicator need an operator-visible
trace: error class, timeout stage, provider status, elapsed time, and token
counts. The column is nullable so historical holds stay unchanged, and the
payload is structured metadata only — never source, prompts, or model text.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c91e4b7a2d08"
down_revision: str | Sequence[str] | None = "e804a171db92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the court trace without rewriting existing quarantine rows."""
    op.add_column(
        "screening_quarantines",
        sa.Column(
            "court_diagnostic",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove the court trace."""
    op.drop_column("screening_quarantines", "court_diagnostic")

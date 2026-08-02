"""Add build_only to screening_attempts.

Revision ID: d2e4f6a80b13
Revises: a1c7e93f2b40
Create Date: 2026-07-21 03:15:00.000000

A build-only screening attempt rebuilds an already-adjudicated submission's
missing prerequisites (screened image / dataset) without re-running the
anti-cheat source review. ``NOT NULL DEFAULT false`` so every existing attempt
keeps the normal full-review semantics.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e4f6a80b13"
down_revision: str | Sequence[str] | None = "a1c7e93f2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_attempts",
        sa.Column(
            "build_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("screening_attempts", "build_only")

"""add replay protection for signed validator requests

Revision ID: f4c8a1d72e09
Revises: f6c2d0e35a41
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4c8a1d72e09"
down_revision: str | Sequence[str] | None = "f6c2d0e35a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE validator_request_nonces (
            nonce UUID PRIMARY KEY,
            validator_hotkey TEXT NOT NULL,
            used_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX validator_request_nonces_expires_at_idx "
        "ON validator_request_nonces (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS validator_request_nonces")

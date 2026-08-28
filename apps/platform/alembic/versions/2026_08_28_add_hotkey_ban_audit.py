"""add append-only hotkey ban audit

Revision ID: 8d2e1f4c9a70
Revises: f2a91c48d7e5
Create Date: 2026-08-28 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8d2e1f4c9a70"
down_revision: str | Sequence[str] | None = "f2a91c48d7e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hotkey_ban_audit (
            seq BIGSERIAL PRIMARY KEY,
            hotkey TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            previous_reason TEXT,
            previous_banned_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT hotkey_ban_audit_action_check CHECK (action = 'unban')
        )
        """
    )
    op.execute(
        "CREATE INDEX hotkey_ban_audit_hotkey_recorded_idx "
        "ON hotkey_ban_audit (hotkey, recorded_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hotkey_ban_audit")

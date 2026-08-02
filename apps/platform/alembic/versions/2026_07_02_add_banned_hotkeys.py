"""add the banned_hotkeys table

Revision ID: a3f1c9d27b40
Revises: 9b2e4c7a1f38
Create Date: 2026-07-02 15:00:00.000000

Adds a hotkey-level ban list (C4). This is distinct from the per-agent
``agentstatus = 'banned'`` value, which rejects a single submission: a row here
blocks a *miner* — every future upload from the hotkey — and is the enforcement
point the deferred upload/retrieval ban checks needed.

- ``banned_hotkeys.hotkey`` — SS58 hotkey, primary key (banned at most once).
- ``banned_hotkeys.reason`` — nullable audit note.
- ``banned_hotkeys.banned_at`` — insert timestamp (server default now()).

Populated out-of-band by the owner (``scripts/ban_hotkey.py``); there is no
public write surface.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c9d27b40"
down_revision: str | Sequence[str] | None = "9b2e4c7a1f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the banned_hotkeys table."""
    op.execute(
        """
        CREATE TABLE banned_hotkeys (
            hotkey    TEXT PRIMARY KEY,
            reason    TEXT,
            banned_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop the banned_hotkeys table."""
    op.execute("DROP TABLE IF EXISTS banned_hotkeys")

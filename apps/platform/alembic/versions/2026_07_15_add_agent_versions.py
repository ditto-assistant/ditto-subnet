"""add immutable per-name versions for new agent submissions

Revision ID: c73a1e5f9b24
Revises: a2d7f4c81e93
Create Date: 2026-07-15 09:40:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c73a1e5f9b24"
down_revision: str | Sequence[str] | None = "a2d7f4c81e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows deliberately remain NULL: miners historically encoded
    # revisions inconsistently in the free-form name, so inferring a sequence
    # would create false lineage. The application assigns v1+ after this migration.
    op.execute("ALTER TABLE agents ADD COLUMN version INTEGER")
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT agents_version_positive_check "
        "CHECK (version IS NULL OR version > 0)"
    )
    op.execute(
        "ALTER TABLE agents ADD CONSTRAINT agents_hotkey_name_version_key "
        "UNIQUE (miner_hotkey, name, version)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_hotkey_name_version_key"
    )
    op.execute(
        "ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_version_positive_check"
    )
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS version")

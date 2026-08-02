"""add exact cross-miner duplicate precheck evidence

Revision ID: a31d8b4c9e72
Revises: f1a7c3d92b84
Create Date: 2026-07-15 01:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a31d8b4c9e72"
down_revision: str | Sequence[str] | None = "f1a7c3d92b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN screening_reason_code TEXT")
    op.execute("ALTER TABLE screening_attempts ADD COLUMN reason_code TEXT")
    op.execute("ALTER TABLE screening_attempts ADD COLUMN duplicate_of UUID")
    op.execute(
        "ALTER TABLE screening_attempts ADD CONSTRAINT "
        "screening_attempts_duplicate_of_fkey FOREIGN KEY (duplicate_of) "
        "REFERENCES agents(agent_id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE screening_attempts ADD CONSTRAINT "
        "screening_attempts_reason_code_check CHECK (reason_code IS NULL OR "
        "reason_code ~ '^[a-z0-9][a-z0-9-]{0,63}$')"
    )
    op.execute("CREATE INDEX agents_sha256_idx ON agents (sha256)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS agents_sha256_idx")
    op.execute(
        "ALTER TABLE screening_attempts DROP CONSTRAINT IF EXISTS "
        "screening_attempts_reason_code_check"
    )
    op.execute(
        "ALTER TABLE screening_attempts DROP CONSTRAINT IF EXISTS "
        "screening_attempts_duplicate_of_fkey"
    )
    op.execute("ALTER TABLE screening_attempts DROP COLUMN IF EXISTS duplicate_of")
    op.execute("ALTER TABLE screening_attempts DROP COLUMN IF EXISTS reason_code")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS screening_reason_code")

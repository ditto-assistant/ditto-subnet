"""add ledger + anti-copy columns and the scored partial index

Revision ID: 9b2e4c7a1f38
Revises: 7c1f2a9d4e50
Create Date: 2026-07-02 12:00:00.000000

Supports the KOTH+ATH incentive mechanism. The validator computes weights from a
persistent *best-eligible-score-per-miner* ledger (``GET /scoring/scores``) instead
of the transient ``evaluating`` sweep, so a scored agent no longer falls to zero the
epoch after it is scored. This migration adds the columns that ledger + its anti-copy
moderation gate need:

- ``agents.size_bytes`` — tarball size captured at upload; a cheap near-dup signal
  (a lightly-tweaked copy has a near-identical size + score).
- ``agents.duplicate_of`` / ``agents.review_reason`` — the moderation record written
  when a suspicious high-scorer is held in ``ath_pending_review`` for review.
- ``scores.signature`` — the validator's sr25519 signature over the report, persisted
  so the exposed ledger is self-verifying (a validator cannot repudiate, and the
  platform cannot fabricate, a score). Backfill-nullable for rows written before this.
- ``agents_status_scored_idx`` — partial index backing the ledger read (mirrors the
  existing ``evaluating`` / ``uploaded`` partial indexes).

No enum change: ``ath_pending_review`` already exists in the ``agentstatus`` type.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b2e4c7a1f38"
down_revision: str | Sequence[str] | None = "7c1f2a9d4e50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ledger/anti-copy columns + the scored-queue partial index."""
    op.execute("ALTER TABLE agents ADD COLUMN size_bytes BIGINT")
    op.execute(
        """
        ALTER TABLE agents
            ADD COLUMN duplicate_of UUID
                REFERENCES agents (agent_id) ON DELETE SET NULL
        """
    )
    op.execute("ALTER TABLE agents ADD COLUMN review_reason TEXT")
    op.execute("ALTER TABLE scores ADD COLUMN signature TEXT")
    op.execute(
        """
        CREATE INDEX agents_status_scored_idx
            ON agents (status)
            WHERE status = 'scored'
        """
    )


def downgrade() -> None:
    """Reverse the ledger/anti-copy schema additions."""
    op.execute("DROP INDEX IF EXISTS agents_status_scored_idx")
    op.execute("ALTER TABLE scores DROP COLUMN IF EXISTS signature")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS review_reason")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS duplicate_of")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS size_bytes")

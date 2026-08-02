"""add bounded review payloads to screening quarantines

Revision ID: a2d7f4c81e93
Revises: b42e9c5d1f83
Create Date: 2026-07-14 12:00:00.000000

Quarantine rows previously carried only ``reason_code`` plus evidence digests,
so the operator console (Backroom) reviewed submissions nearly blind. The
screener now ships two bounded payloads with a quarantine verdict and the
platform persists them for the review console:

- ``screening_quarantines.evidence`` — JSONB list of public-safe policy
  evidence items ``{module_id, code, summary, digest}`` (max 16, summaries
  capped at 240 chars). Display data over the authenticated screener channel.
- ``screening_quarantines.finding`` — JSONB source-review finding
  ``{artifact_sha256, prompt_revision, risk_level, confidence, categories,
  evidence[{path,line,category}], summary}``. Its canonical JSON hashes to the
  signed ``finding_digest``, so the console can verify it end to end.

Both nullable: backfill-null for quarantines recorded before this landed and
for verdicts from older screeners.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2d7f4c81e93"
down_revision: str | Sequence[str] | None = "b42e9c5d1f83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the review payload columns and console lookup indexes."""
    op.execute("ALTER TABLE screening_quarantines ADD COLUMN evidence JSONB")
    op.execute("ALTER TABLE screening_quarantines ADD COLUMN finding JSONB")
    # The review console's duplicate and miner-history lookups must not scan.
    op.execute(
        "CREATE INDEX agents_normalized_source_hash_idx "
        "ON agents(normalized_source_hash)"
    )
    op.execute(
        "CREATE INDEX screening_quarantines_agent_idx "
        "ON screening_quarantines(agent_id)"
    )


def downgrade() -> None:
    """Drop the review payload columns and console lookup indexes."""
    op.execute("DROP INDEX screening_quarantines_agent_idx")
    op.execute("DROP INDEX agents_normalized_source_hash_idx")
    op.execute("ALTER TABLE screening_quarantines DROP COLUMN finding")
    op.execute("ALTER TABLE screening_quarantines DROP COLUMN evidence")

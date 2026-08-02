"""add agents.structural_fingerprint for AST-level anti-copy detection

Revision ID: d5f2a3b91e64
Revises: c4e8b1a06d72
Create Date: 2026-07-05 18:00:00.000000

Adds the structural (AST-level) channel to the anti-copy gate, complementing the
lexical ``content_fingerprint`` (revision ``c4e8b1a06d72``). The lexical sketch is
computed by the platform at upload and survives reformatting + localized edits;
the structural sketch is computed by **dittobench** (the only place the crate is
unpacked and a Rust parser is available), hashes only the *shape* of the parse
tree, and so additionally survives identifier renaming. It arrives on the score
report (``POST /validator/agent/{id}/score``) as advisory, unsigned moderation
metadata and is persisted here for cross-miner comparison in the gate.

- ``agents.structural_fingerprint`` — JSONB sketch object ``{v, k, card, m}``, the
  same shape as ``content_fingerprint`` so both channels use one comparison path.
  Nullable: null for agents scored before this landed, submissions built on the
  local harness_url path (no crate to parse), or crates with no parseable Rust
  (the gate reads null as "no structural match").

No index: read only for the small best-eligible-per-miner ledger the gate scans.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f2a3b91e64"
down_revision: str | Sequence[str] | None = "c4e8b1a06d72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``agents.structural_fingerprint`` JSONB column."""
    op.execute("ALTER TABLE agents ADD COLUMN structural_fingerprint JSONB")


def downgrade() -> None:
    """Drop ``agents.structural_fingerprint``."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS structural_fingerprint")

"""add agents.normalized_source_hash for exact-repack anti-copy detection

Revision ID: e6a3c4d20f81
Revises: d5f2a3b91e64
Create Date: 2026-07-07 12:00:00.000000

Adds the "exact-repack" channel to the anti-copy gate
(:mod:`ditto.api_server.scoring_gate`), on top of the lexical
``content_fingerprint`` (``c4e8b1a06d72``) and structural
``structural_fingerprint`` (``d5f2a3b91e64``). The value is a single hash of the
crate's *canonicalized* source — comments stripped, whitespace removed, files
sorted (:func:`ditto.api_server.fingerprint.compute_normalized_source_hash`) —
computed by the platform at upload. It is an *equality* signal: a copy that only
reformats, re-comments, or reorders/renames files hashes to the same value even
though its ``sha256`` and shingle sketches differ. Unlike the score-proximity
fingerprint rules, an exact match holds unconditionally, mirroring the exact
``sha256`` rule.

- ``agents.normalized_source_hash`` — hex ``TEXT`` (SHA-256), nullable. Null for
  rows uploaded before this landed and for tarballs that are unreadable/empty at
  upload (the gate reads null as "no repack match").

No index: read only for the small best-eligible-per-miner ledger the gate scans,
never filtered on in SQL.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a3c4d20f81"
down_revision: str | Sequence[str] | None = "d5f2a3b91e64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``agents.normalized_source_hash`` TEXT column."""
    op.execute("ALTER TABLE agents ADD COLUMN normalized_source_hash TEXT")


def downgrade() -> None:
    """Drop ``agents.normalized_source_hash``."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS normalized_source_hash")

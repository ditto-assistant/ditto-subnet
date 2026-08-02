"""add agents.prompt_fingerprint for prompt-surface anti-copy detection

Revision ID: f7b4d5e31a92
Revises: e6a3c4d20f81
Create Date: 2026-07-07 18:00:00.000000

Adds the prompt-surface channel to the anti-copy stack
(``docs/SEMANTIC-CLONE-PREVENTION.md`` §4), on top of the lexical
``content_fingerprint`` (``c4e8b1a06d72``), structural ``structural_fingerprint``
(``d5f2a3b91e64``), and normalized-source ``normalized_source_hash``
(``e6a3c4d20f81``). The value is a word-shingle MinHash sketch of the crate's
prompt-length string literals
(:func:`ditto.api_server.fingerprint.compute_prompt_fingerprint`), computed by the
platform at upload. Because it hashes string *contents*, it survives the
identifier renaming that defeats the lexical and normalized-source channels.

This lands the sketch in **shadow mode**: it is computed and stored for every
agent (so it is available for calibration and retroactive analysis) and surfaced
on the eligible ledger, but it does not yet create a hold on its own. A prompt
match alone is not copy evidence — honest agents on the same reference harness
share scaffolding prompts (the convergent case in ``ditto.anticopy.calibration``),
and the signals orthogonal to that convergence (behavioral / code-embedding) are
not built yet. The active fusion hold is deferred to S2/S3.

- ``agents.prompt_fingerprint`` — JSONB sketch object ``{v, k, card, m}`` (``v`` is
  the string ``"p1"``, isolating it from the lexical/structural channels).
  Nullable: null for rows written before this landed and for crates with no
  prompt-length literal or an unreadable tarball.

No index: read only for the small best-eligible-per-miner ledger the gate scans.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b4d5e31a92"
down_revision: str | Sequence[str] | None = "e6a3c4d20f81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``agents.prompt_fingerprint`` JSONB column."""
    op.execute("ALTER TABLE agents ADD COLUMN prompt_fingerprint JSONB")


def downgrade() -> None:
    """Drop ``agents.prompt_fingerprint``."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS prompt_fingerprint")

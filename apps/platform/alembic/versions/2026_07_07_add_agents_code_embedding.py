"""add agents.code_embedding + code_embed_model for the code-embedding anti-copy signal

Revision ID: a1c9e2b47d53
Revises: f7b4d5e31a92
Create Date: 2026-07-07 20:00:00.000000

Adds the code-embedding channel to the anti-copy stack
(``docs/SEMANTIC-CLONE-PREVENTION.md`` §4). A self-hosted code-embedding service
(text-embeddings-inference) embeds the crate's canonical source
(:func:`ditto.api_server.fingerprint.compute_embedding_input`) at upload; the
resulting unit-norm vector is compared by cosine in the gate. Unlike the lexical /
structural / prompt channels, a code embedder gives high similarity to a renamed
and refactored copy but low similarity to a genuinely different agent, so it is the
first signal orthogonal to convergence — the one that unblocks the prompt-fusion
hold.

Stored in **shadow mode**: computed and stored for every agent (calibration +
retroactive analysis, and available to the gate) but not yet a hold trigger. It is
disabled by default — with no ``CODE_EMBEDDER_URL`` the platform stores null.

- ``agents.code_embedding`` — JSONB array of floats (the unit-norm vector).
  Nullable: null before this landed, when the embedder is disabled, or on any
  best-effort embed failure.
- ``agents.code_embed_model`` — TEXT ``model@revision`` provenance tag, so a model
  change is detectable and can drive a re-embed sweep, and so the gate only compares
  vectors from the same model (a cross-model cosine is meaningless). Nullable
  alongside the vector.

No index: read only for the small best-eligible-per-miner ledger the gate scans.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9e2b47d53"
down_revision: str | Sequence[str] | None = "f7b4d5e31a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``agents.code_embedding`` + ``code_embed_model`` columns."""
    op.execute("ALTER TABLE agents ADD COLUMN code_embedding JSONB")
    op.execute("ALTER TABLE agents ADD COLUMN code_embed_model TEXT")


def downgrade() -> None:
    """Drop the code-embedding columns."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS code_embedding")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS code_embed_model")

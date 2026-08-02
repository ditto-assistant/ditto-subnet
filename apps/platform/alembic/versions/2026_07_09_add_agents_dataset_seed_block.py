"""add agents.dataset_seed_block / dataset_seed_block_hash

Revision ID: e5b1c9d24f30
Revises: d3a9f5e17c24
Create Date: 2026-07-09 17:00:00.000000

The on-chain block reference the per-submission ``dataset_seed`` is derived from
(see :mod:`ditto.api_server.onchain_seed`). At job-ready the platform reads the
latest finalized block and derives ``seed = f(block_hash, agent_id)`` instead of
drawing a local CSPRNG value; pinning the block ``(number, hash)`` lets anyone
recompute and verify the seed, so it is provably not platform-chosen.

- ``agents.dataset_seed_block`` — ``BIGINT``, nullable (block number).
- ``agents.dataset_seed_block_hash`` — hex ``TEXT`` (block hash), nullable.

Both null until job-ready, or on the fallback path when chain-derivation is
unavailable. No index: read by agent_id with the rest of the agent row.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b1c9d24f30"
down_revision: str | Sequence[str] | None = "d3a9f5e17c24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two on-chain seed-provenance columns to ``agents``."""
    op.execute("ALTER TABLE agents ADD COLUMN dataset_seed_block BIGINT")
    op.execute("ALTER TABLE agents ADD COLUMN dataset_seed_block_hash TEXT")


def downgrade() -> None:
    """Drop the on-chain seed-provenance columns."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dataset_seed_block_hash")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dataset_seed_block")

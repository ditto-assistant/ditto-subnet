"""Independent re-derivation of the on-chain dataset seed (prod hardening P2).

The platform derives every agent's dataset seed from an on-chain block hash
pinned after the submission committed, bound to the agent id:

    seed = int(SHA-256(normalized_block_hash || ":" || agent_id)[:8]) & (2**63-1)

That derivation is what makes the seed unpredictable (the block postdates the
commit) and per-agent (two agents pinned at the same block still get distinct
datasets, closing the answer-sharing channel). But until now only the platform
computed it: a validator took ``job.seed`` on faith. This module is the
validator's own copy of the derivation — reproduced verbatim from the
platform's ``onchain_seed.py`` so there is no shared dependency to trust — and
the worker refuses any ticket whose seed does not re-derive from its pinned
block hash. With the check in place, a platform that ground seeds (to favour
or disfavour an agent) would be caught by every honest validator.

Keep byte-compatible with ditto-platform ``ditto/api_server/onchain_seed.py``;
the cross-repo test vector in ``ditto/tests/validator/test_onchain_seed.py``
pins the pairing.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

# Non-negative signed-64-bit mask, matching gen.FreshSeed and the platform.
_INT63_MASK = (1 << 63) - 1


def normalize_block_hash(block_hash: str) -> str:
    """Canonical form of a block hash for hashing: lowercase, no ``0x`` prefix."""
    h = block_hash.strip().lower()
    return h[2:] if h.startswith("0x") else h


def derive_seed(block_hash: str, agent_id: UUID) -> int:
    """The deterministic non-negative int64 dataset seed for (block, agent)."""
    digest = hashlib.sha256(
        f"{normalize_block_hash(block_hash)}:{agent_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & _INT63_MASK


def seed_matches(block_hash: str, agent_id: UUID, seed: int) -> bool:
    """Whether a ticket's pinned seed re-derives from its pinned block hash."""
    return derive_seed(block_hash, agent_id) == seed

"""Verifiable on-chain dataset-seed derivation.

The per-submission dataset seed is derived from an **on-chain block hash** rather
than a platform-local CSPRNG. This removes the last platform-trust assumption in
the scoring pipeline: with a local ``secrets.randbits`` seed a miner had to trust
that the platform did not grind seeds (to favour or disfavour an agent) or leak
them; a chain-derived seed is instead:

* **Unpredictable** — the seed binds to the latest finalized block *at job-ready*,
  which is causally after the miner has already committed their submission
  (upload -> payment -> screening -> pass). The miner could not have known that
  block hash when they submitted, so they cannot pre-compute their dataset.
* **Verifiable** — the derivation is a pure function of the pinned block hash,
  agent id, and (for quorum scoring) validator hotkey. Anyone can fetch the
  block, recompute the seed, and confirm the platform did not choose it.

The seed stays in the non-negative int64 range every downstream consumer expects
(``agents.dataset_seed`` / ``scores.seed`` columns, dittobench-api's regeneration
path), so nothing else changes.

A stronger anti-grind refinement (bind to a *future* block a fixed number of
blocks ahead of the commit, so even the platform's choice of "when to read" is
removed) is a compatible future step: the stored block reference already makes
any such change auditable.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

# The seed is a non-negative signed-64-bit int, matching gen.FreshSeed's masking
# and the DB columns that store it.
_INT63_MASK = (1 << 63) - 1


def normalize_block_hash(block_hash: str) -> str:
    """Canonical form of a block hash for hashing: lowercase, no ``0x`` prefix.

    Pylon may return the hash with or without the prefix; normalizing keeps the
    derivation stable so an independent verifier reproduces the same seed
    regardless of which form they fetched.
    """
    h = block_hash.strip().lower()
    return h[2:] if h.startswith("0x") else h


def derive_seed(block_hash: str, agent_id: UUID) -> int:
    """Derive the deterministic non-negative int64 dataset seed.

    ``seed = int(SHA-256(normalized_block_hash || ":" || agent_id)[:8]) & (2**63-1)``.

    Binding the agent id means two agents that happen to be pinned at the same
    block still get distinct datasets, and the value is reproducible by anyone
    holding the (public) block hash and agent id.
    """
    digest = hashlib.sha256(
        f"{normalize_block_hash(block_hash)}:{agent_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & _INT63_MASK


def derive_validator_seed(
    block_hash: str, agent_id: UUID, validator_hotkey: str
) -> int:
    """Derive the stable dataset seed for one validator's quorum run."""
    digest = hashlib.sha256(
        f"{normalize_block_hash(block_hash)}:{agent_id}:{validator_hotkey}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & _INT63_MASK

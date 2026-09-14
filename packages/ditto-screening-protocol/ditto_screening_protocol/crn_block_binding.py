"""Finalized-block binding constants shared by every CRN seed derivation.

The champion-anchored confirmation CRN family is derived twice -- on the
validator (``ditto/validator/crn.py``) and on Platform
(``apps/platform/ditto/api_server/crn.py``) -- and the two must stay
byte-for-byte aligned. The *floor* from which the family hashes a finalized
chain block, the anchor *delta*, and the hash *normalization* are consensus
inputs too, so they live here once and both copies import them instead of
retyping them (the bench-version-bump rule: one exported constant every layer
imports, never a mirrored literal).
"""

from __future__ import annotations

CRN_BLOCK_BINDING_MIN_BENCH_VERSION = 13
"""Floor from which confirmation CRN seeds hash a Platform-pinned finalized
block. A floor, never an enumeration: every later version binds. Versions
below it keep the legacy ``block_hash=None`` derivation byte-for-byte, so an
in-flight v12 reign's seeds never move."""

CRN_ANCHOR_BLOCK_DELTA = 10
"""Blocks past the ready block Platform waits before it pins the anchor: the
hash of ``B_ready + Δ`` is unknown to everyone -- Platform included -- when the
reign becomes ready, so "when to read" is not a choice anyone can grind."""


def normalize_block_hash(block_hash: str) -> str:
    """Canonical hash text for hashing: lowercase, no ``0x``.

    Pylon and Substrate return either form; the derivation must not depend on
    which, or two validators reading the same block would derive two families.
    """
    h = block_hash.strip().lower()
    return h[2:] if h.startswith("0x") else h

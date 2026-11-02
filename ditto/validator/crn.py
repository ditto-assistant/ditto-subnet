"""Common Random Numbers (CRN) seed derivation for KOTH re-scoring.

To compare the champion against challengers on equal
footing, they must be scored on the **same** freshly-generated dataset — then the
variance of their score *difference* (the only quantity KOTH cares about)
collapses by the covariance term instead of summing.

The seed must be a **deterministic function of the comparison**, not a per-
validator random draw, or every validator would re-score on a different dataset,
resubmit different composites, and break Yuma consensus. So it is a pure hash of
the (sorted) set of agent ids being compared plus the bench_version:

    crn_seed = sha256( sorted(agent_ids) ‖ bench_version )  → non-negative int63

Every validator scoring the same set at the same version derives the same seed →
the same dataset → comparable composites. It is still anti-cheat: the seed
depends on agent ids not known before submission and rotates per pairing/version,
so nothing can be precomputed. The int63 masking mirrors ``gen.FreshSeed`` on the
dittobench-api side so the value round-trips through the wire unchanged.

**Version-scoped + single source of truth.** ``crn_seed`` is keyed on the major
bench ``version`` (v4, v5, …), so each version has its own seed family and a
version bump cleanly starts a fresh confirmation baseline. This encoding is the
**single source of truth for both repos**: the ditto-platform mirrors it
byte-for-byte and, for the top-5 confirmation lane (ditto-platform #280),
**validates every submitted confirmation seed** against its own derivation from
``(champion_id, version, k)`` — so a validator cannot cherry-pick a favorable
seed (anti-grind). Any change here must land identically on the platform.

**Block binding (bench v13+).** A pure function of public ids is also a pure
function a *miner* can evaluate: the champion's agent id is on the public board,
so ``sha256(champion ‖ version ‖ k)`` names the champion-anchored confirmation
datasets to anyone before they are ever leased, and a challenger can train on
them. From :data:`CRN_BLOCK_BINDING_MIN_BENCH_VERSION` the derivation takes one
more input the miner cannot know at submission time: the hash of a **finalized
chain block** that Platform pins per reign (``B_ready + Δ``, ``Δ =``
:data:`CRN_ANCHOR_BLOCK_DELTA`, after a finality wait). Platform serves the pin
on every confirmation lease and on the ledger; validators re-derive
``crn_seed(..., block_hash=...)`` and refuse a lease whose seed does not
reproduce, exactly like the P2 check on ``derive_validator_seed``. That check
proves the seed is *consistent with the pin Platform served*; the validator
does not read the pinned hash back from the chain (its ``ChainClient`` has no
block-hash read), so fleet agreement is agreement with Platform's pin, not an
independent chain verification -- a follow-up. With ``block_hash=None`` the
encoding is **byte-identical** to the legacy derivation, so every v<=12 seed
family is unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ditto_screening_protocol.crn_block_binding import (
    CRN_ANCHOR_BLOCK_DELTA as _SHARED_CRN_ANCHOR_BLOCK_DELTA,
)
from ditto_screening_protocol.crn_block_binding import (
    CRN_BLOCK_BINDING_MIN_BENCH_VERSION as _SHARED_CRN_BLOCK_BINDING_MIN_BENCH_VERSION,
)
from ditto_screening_protocol.crn_block_binding import normalize_block_hash

# Mask to a non-negative signed-63-bit integer, matching dittobench-api's
# FreshSeed (``int64(uint64 >> 1)``): JSON-clean and never negative.
_INT63_MASK = (1 << 63) - 1

# The binding floor and anchor delta are consensus inputs shared with the other
# CRN copy; both import the one exported constant instead of retyping it. Read
# through this module at call time (tests lower the floor to a fixture era).
CRN_BLOCK_BINDING_MIN_BENCH_VERSION = _SHARED_CRN_BLOCK_BINDING_MIN_BENCH_VERSION
CRN_ANCHOR_BLOCK_DELTA = _SHARED_CRN_ANCHOR_BLOCK_DELTA


def crn_block_binding_active(version: int) -> bool:
    """Whether confirmation seeds at ``version`` must carry a block binding."""
    return int(version) >= CRN_BLOCK_BINDING_MIN_BENCH_VERSION


def crn_seed(
    agent_ids: Iterable[str],
    *,
    version: int,
    k: int = 0,
    block_hash: str | None = None,
) -> int:
    """Deterministic dataset seed for a CRN comparison over ``agent_ids`` at
    ``version``. Order-independent (the *set* of compared agents determines the
    seed) and pure, so every validator computes the same value.

    ``k`` indexes a confirmation replicate (prod hardening P4): a dethrone-grade
    comparison runs the compared agents on ``K`` common seeds (k = 0..K-1) and
    the weight fold takes each agent's median composite over them, so a single
    lucky draw cannot flip the crown. ``k=0`` is byte-identical to the original
    single-seed derivation, so existing pins and mixed fleets stay consistent.

    ``block_hash`` binds the seed to a finalized chain block Platform pinned for
    the reign (bench v13+). ``None`` is the legacy encoding, unchanged to the
    byte; a bound seed appends a ``\\x00block`` tag and the normalized hash, so
    no legacy seed can collide with a bound one by construction.
    """
    h = hashlib.sha256()
    for aid in sorted(agent_ids):
        h.update(aid.encode("utf-8"))
        h.update(b"\x00")
    h.update(str(int(version)).encode("ascii"))
    if k > 0:
        h.update(b"\x00k")
        h.update(str(int(k)).encode("ascii"))
    if block_hash is not None:
        normalized = normalize_block_hash(block_hash)
        if not normalized:
            raise ValueError("a block-bound CRN seed requires a non-empty block hash")
        h.update(b"\x00block")
        h.update(normalized.encode("ascii"))
    return int.from_bytes(h.digest()[:8], "little", signed=False) & _INT63_MASK


def confirmation_seeds(
    agent_ids: Iterable[str],
    *,
    version: int,
    count: int,
    block_hash: str | None = None,
) -> list[int]:
    """The ``count`` common confirmation seeds for one comparison, k = 0..count-1.
    ``count <= 1`` degrades to the single classic CRN seed, so a fleet running
    ``K=1`` is byte-identical to the pre-P4 single-seed sweep. ``block_hash``
    threads the reign's finalized-block binding through every replicate."""
    ids = list(agent_ids)
    n = max(1, int(count))
    return [
        crn_seed(ids, version=version, k=k, block_hash=block_hash) for k in range(n)
    ]

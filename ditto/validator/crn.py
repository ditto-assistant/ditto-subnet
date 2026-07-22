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
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

# Mask to a non-negative signed-63-bit integer, matching dittobench-api's
# FreshSeed (``int64(uint64 >> 1)``): JSON-clean and never negative.
_INT63_MASK = (1 << 63) - 1


def crn_seed(agent_ids: Iterable[str], *, version: int, k: int = 0) -> int:
    """Deterministic dataset seed for a CRN comparison over ``agent_ids`` at
    ``version``. Order-independent (the *set* of compared agents determines the
    seed) and pure, so every validator computes the same value.

    ``k`` indexes a confirmation replicate (prod hardening P4): a dethrone-grade
    comparison runs the compared agents on ``K`` common seeds (k = 0..K-1) and
    the weight fold takes each agent's median composite over them, so a single
    lucky draw cannot flip the crown. ``k=0`` is byte-identical to the original
    single-seed derivation, so existing pins and mixed fleets stay consistent.
    """
    h = hashlib.sha256()
    for aid in sorted(agent_ids):
        h.update(aid.encode("utf-8"))
        h.update(b"\x00")
    h.update(str(int(version)).encode("ascii"))
    if k > 0:
        h.update(b"\x00k")
        h.update(str(int(k)).encode("ascii"))
    return int.from_bytes(h.digest()[:8], "little", signed=False) & _INT63_MASK


def confirmation_seeds(
    agent_ids: Iterable[str], *, version: int, count: int
) -> list[int]:
    """The ``count`` common confirmation seeds for one comparison, k = 0..count-1.
    ``count <= 1`` degrades to the single classic CRN seed, so a fleet running
    ``K=1`` is byte-identical to the pre-P4 single-seed sweep."""
    ids = list(agent_ids)
    n = max(1, int(count))
    return [crn_seed(ids, version=version, k=k) for k in range(n)]

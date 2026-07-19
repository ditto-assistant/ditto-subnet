"""Independent re-derivation of the reproduce-under-transform audit (v3 Part A).

A share of every run's cases is re-asked under a transform the miner could not
see coming: both WHICH cases are audited and WHICH transform each gets are pure
functions of the dataset seed, and that seed derives from a block hash that
postdates the submission's commit (``ditto/validator/onchain_seed.py``).

This module is the validator's own copy of that derivation, reproduced from
dittobench-datagen ``persona/transform.go``. As with the on-chain seed, the
point is that a validator takes nothing on faith: it can regenerate the audit
set itself and check that the metric it is acting on is the one the public
inputs imply.

Keep byte-compatible with ``persona/transform.go``; the cross-repo vectors in
``ditto/tests/validator/test_transform_audit.py`` were emitted by the Go
implementation and pin the pairing.

Honest scope, repeated here because it governs how a verdict may be used: a
directional audit result is the SURFACE-BRITTLENESS signature (competent on the base
phrasing, wrong under an unpredictable rephrasing) or MEMORIZATION (right answer
for the base, stale answer under a covariance shift). It is not evidence about a
robust local solver, which recomputes correctly under the transform too. Do not
describe a failed audit as proof of cheating; it is a quarantine trigger for
operator review, which is why the platform holds rather than bans.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

# Public constants. These are part of the derivation contract, not tunables: a
# validator that changed one would compute a different audit set from every
# other validator and its verdict would not reproduce.
AUDIT_BPS = 1500
"""Per-case audit rate in basis points (15%). Mirrors persona.AuditBps."""

TRANSFORM_SPACE = 4096
"""Size of the transform id space. Mirrors persona.TransformSpace."""

AUDIT_DOMAIN = "audit-v3"
TRANSFORM_DOMAIN = "xform-v3"

AUDIT_TWIN_PREFIX = "auditxf-"
"""TwinGroup prefix marking an audit pair. Mirrors persona.AuditTwinPrefix."""


def _audit_hash(seed: int, case_id: str, domain: str) -> int:
    """SHA-256 over the ':'-joined public inputs, first 8 bytes big-endian.

    Same convention as ``onchain_seed.derive_seed`` so both derivations are
    trivial to mirror in any language.
    """
    digest = hashlib.sha256(f"{seed}:{case_id}:{domain}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def audit_selected(seed: int, case_id: str) -> bool:
    """Whether a case is picked for the transform audit."""
    return _audit_hash(seed, case_id, AUDIT_DOMAIN) % 10000 < AUDIT_BPS


def audit_transform_id(seed: int, case_id: str) -> int:
    """The transform id a case is audited under."""
    return _audit_hash(seed, case_id, TRANSFORM_DOMAIN) % TRANSFORM_SPACE


# --- brittleness verdict ------------------------------------------------------
#
# The verdict is a one-sided exact BINOMIAL TEST on the discordant audit pairs,
# not a threshold on a rate. The 2026-07-18 calibration is why.
#
# A pair the harness answered correctly in the base phrasing and incorrectly
# under the post-commit transform is the brittleness event (``base_only``). The
# mirror image (``transform_only``) is not: a surface-keyed lookup has no reason
# to produce it. So the null hypothesis is that discordant pairs fall either way
# with equal probability, which is exactly what an honest nondeterministic model
# does. Measured: honest 5 base-only vs 6 transform-only (symmetric); a
# surface-gated harness 6 vs 0.
#
# Testing that null has a property a threshold on a rate does not: ALPHA *is*
# the false-positive rate on honest miners, by construction. A rate threshold
# has an unknown error rate that has to be discovered empirically, and the one
# we had discovered was terrible (a 0.70 floor flagged 16% of honest runs while
# catching almost no brittle ones).
ALPHA = 0.01
"""Max probability of flagging an honest harness, per verdict."""

MIN_DISCORDANT = 6
"""Fewest discordant pairs that can produce a verdict.

Below this the exact test cannot reach ALPHA even on a perfect run of
base-only pairs (0.5**5 = 0.031 > 0.01), so a verdict is refused outright
rather than computed and silently ignored.
"""


def binomial_tail(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact, no dependencies."""
    if n <= 0:
        return 1.0
    k = max(0, k)
    total = 0.0
    coeff = 1.0  # C(n, 0)
    for i in range(0, n + 1):
        if i >= k:
            total += coeff * (p**i) * ((1 - p) ** (n - i))
        coeff = coeff * (n - i) / (i + 1)
    return min(1.0, total)


def brittleness_pvalue(base_only: int, transform_only: int) -> float:
    """One-sided p-value that base-only pairs exceed chance.

    Only discordant pairs enter the test. Both-correct and both-wrong pairs are
    excluded on purpose: both-wrong is the large majority on a hard benchmark
    (81% in calibration) and reflects accuracy, which the composite already
    scores, not brittleness.
    """
    n = base_only + transform_only
    if n <= 0:
        return 1.0
    return binomial_tail(base_only, n)


def audit_pair_counts(details: Mapping[str, Any] | None) -> dict[str, int] | None:
    """Read the audit 2x2 counts out of a report's ``details``.

    Returns None when the run carried no pairs or the engine predates the
    counts, so an absent measurement is never confused with a failing one.
    """
    if not details:
        return None
    raw = details.get("audit_pairs")
    if not isinstance(raw, Mapping):
        return None
    out = {}
    for key in ("both_correct", "base_only", "transform_only", "both_wrong"):
        v = raw.get(key, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
        out[key] = v
    return out


def pool_audit_pairs(
    details_list: Sequence[Mapping[str, Any] | None],
) -> dict[str, int]:
    """Sum the 2x2 counts across runs.

    Pooling is the point of reporting counts. A full run yields only a handful
    of audit pairs, so no single run can reach ALPHA; the evidence has to be
    accumulated across an agent's runs before a verdict is even attempted.
    """
    pooled = {"both_correct": 0, "base_only": 0, "transform_only": 0, "both_wrong": 0}
    for d in details_list:
        counts = audit_pair_counts(d)
        if not counts:
            continue
        for k in pooled:
            pooled[k] += counts[k]
    return pooled


def brittleness_signature(
    details_list: Sequence[Mapping[str, Any] | None],
    *,
    alpha: float = ALPHA,
    min_discordant: int = MIN_DISCORDANT,
) -> bool:
    """Whether pooled audit evidence shows directional brittleness.

    Returns False whenever the evidence is thin -- no run carried the counts, or
    too few discordant pairs to reach ``alpha``. Absence of evidence is not a
    failed audit, and the cost of getting that backwards is paid by a legitimate
    miner.
    """
    pooled = pool_audit_pairs(details_list)
    discordant = pooled["base_only"] + pooled["transform_only"]
    if discordant < min_discordant:
        return False
    return brittleness_pvalue(pooled["base_only"], pooled["transform_only"]) <= alpha

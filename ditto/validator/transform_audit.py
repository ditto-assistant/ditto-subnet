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

Honest scope, repeated here because it governs how a verdict may be used: a low
robustness value is the SURFACE-BRITTLENESS signature (competent on the base
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


# The robustness floor a run must clear. Calibrated against a REAL locked model,
# never the zero-variance reference router (see docs/calibration-trust.md): a
# floor set above honest-model robustness false-fails legitimate miners, which is
# the main risk this mechanism carries. It sits below honest-model robustness and
# above what a surface-brittle parser achieves.
#
# Provisional pending the calibration sweep recorded in docs/BASELINES.md. Until
# that lands, treat a breach as advisory telemetry (see brittleness_signature).
AUDIT_MIN_ROBUSTNESS = 0.70

# Below this many audit pairs a single split swings the rate too far to act on.
# A short run is not evidence of brittleness, so it is simply not judged.
AUDIT_MIN_PAIRS = 4


def transform_robustness(details: Mapping[str, Any] | None) -> tuple[float | None, int]:
    """Read (robustness, pair_count) out of a report's ``details`` telemetry.

    Returns ``(None, 0)`` when the run carried no audit pairs or the engine did
    not report the fields (an older scoring engine), so an absent metric is
    never confused with a failing one.
    """
    if not details:
        return None, 0
    raw = details.get("transform_robustness")
    if raw is None:
        return None, 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, 0
    try:
        pairs = int(details.get("audit_case_count") or 0)
    except (TypeError, ValueError):
        pairs = 0
    return value, pairs


def brittleness_signature(
    details_list: Sequence[Mapping[str, Any] | None],
    *,
    min_robustness: float = AUDIT_MIN_ROBUSTNESS,
    min_pairs: int = AUDIT_MIN_PAIRS,
) -> bool:
    """Whether a set of finalized reports shows the brittleness signature.

    The judgement is on the MEDIAN robustness across the finalized reports, not
    on any single run, for the same reason the KOTH fold medians composites: one
    run on a nondeterministic model is noisy, and acting on a single low sample
    would quarantine honest miners.

    Returns False whenever there is not enough evidence -- no reports carrying
    the metric, or too few audit pairs behind them. Absence of evidence is not
    a failed audit.
    """
    values = [
        value
        for value, pairs in (transform_robustness(d) for d in details_list)
        if value is not None and pairs >= min_pairs
    ]
    if not values:
        return False
    values.sort()
    mid = len(values) // 2
    median = (
        values[mid] if len(values) % 2 == 1 else (values[mid - 1] + values[mid]) / 2
    )
    return median < min_robustness

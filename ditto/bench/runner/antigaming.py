"""Anti-gaming helpers for DittoBench validators.

Python port of ``go/bittensor/antigaming.go``. Both implementations agree on
SHA-256 hashing + hex-sorted ranking so two validators sharing a secret
produce identical hidden-set partitions, paraphrase seeds, and distractor
pools regardless of language.

The helpers cover the four shared controls documented in
``ditto/bench/docs/anti_gaming.md``:

- :func:`partition_fixture` carves an input case-id list into public,
  private, and canary buckets using a validator-controlled secret.
- :func:`paraphrase_seed` derives a deterministic salt fed to the
  validator's paraphrase generator.
- :func:`memorisation_discount` multiplicatively discounts a miner's
  aggregate weight when their canary score lags their public score.
- :func:`distractor_bundle_for` builds a deterministic distractor pool that
  never overlaps with the expected/forbidden pair IDs of a case.

Parity with the Go reference is enforced by the suite in
``ditto/tests/bench/test_antigaming.py``.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass, field


class CanaryIdenticalError(ValueError):
    """Raised when a candidate paraphrase normalises to its public twin.

    Validators MUST refuse to ship such a paraphrase as a canary because it
    cannot distinguish memorising miners from non-memorising ones.
    """


@dataclass(slots=True)
class HiddenSet:
    """Three-way split of a fixture corpus by visibility bucket.

    Mirrors the Go ``HiddenSet`` struct. Lists are sorted so two validators
    with the same secret produce byte-identical splits on disk.
    """

    public: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)
    canary: list[str] = field(default_factory=list)


def _digest(parts: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``parts`` (UTF-8)."""
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def partition_fixture(
    case_ids: list[str],
    secret: str,
    *,
    private_frac: float,
    canary_frac: float,
) -> HiddenSet:
    """Deterministically split ``case_ids`` into public/private/canary buckets.

    The split is a function of ``(case_ids, secret)``. Rotating the secret
    rotates the partition entirely; running twice with the same secret
    returns the same lists. ``private_frac`` and ``canary_frac`` are clamped
    to ``[0, 0.5]`` each, and their sum is shrunk proportionally so at
    least 20% of cases remain public after rounding. With small input
    sizes we additionally guarantee one public case so honest miners always
    have something to train against.
    """
    if private_frac < 0:
        private_frac = 0.0
    if canary_frac < 0:
        canary_frac = 0.0
    if private_frac > 0.5:
        private_frac = 0.5
    if canary_frac > 0.5:
        canary_frac = 0.5
    if private_frac + canary_frac > 0.8:
        scale = 0.8 / (private_frac + canary_frac)
        private_frac *= scale
        canary_frac *= scale

    scored = [(case_id, _digest(f"{secret}|{case_id}")) for case_id in case_ids]
    scored.sort(key=lambda pair: pair[1])

    n = len(scored)
    private_count = int(round(n * private_frac))
    canary_count = int(round(n * canary_frac))
    if private_count + canary_count >= n and n > 0:
        over = private_count + canary_count - (n - 1)
        if canary_count >= over:
            canary_count -= over
        else:
            over -= canary_count
            canary_count = 0
            private_count -= over

    out = HiddenSet()
    for index, (case_id, _hash) in enumerate(scored):
        if index < private_count:
            out.private.append(case_id)
        elif index < private_count + canary_count:
            out.canary.append(case_id)
        else:
            out.public.append(case_id)

    out.public.sort()
    out.private.sort()
    out.canary.sort()
    return out


def paraphrase_seed(secret: str, case_id: str) -> str:
    """Return a deterministic per-case paraphrase salt.

    Combined with a paraphrase generator on the validator side this gives
    two validators sharing ``secret`` identical canary prompts without
    leaking the secret to miners. The output is a 64-character hex digest.
    """
    return _digest(f"{secret}|paraphrase|{case_id}")


def memorisation_discount(
    public_mean: float,
    canary_mean: float,
    canary_samples: int,
    *,
    gap_threshold: float,
    gap_ceiling: float,
    max_discount: float,
) -> float:
    """Return the multiplicative weight discount applied for memorisation.

    The discount kicks in once ``public_mean - canary_mean`` exceeds
    ``gap_threshold`` and saturates at ``max_discount`` once the gap reaches
    ``gap_ceiling``. With ``canary_samples == 0`` the discount is disabled
    (returns ``1.0``) so validators do not penalise miners before enough
    canary cases have been graded.
    """
    if canary_samples == 0:
        return 1.0
    gap = public_mean - canary_mean
    if gap <= gap_threshold:
        return 1.0
    if gap_ceiling <= gap_threshold:
        gap_ceiling = gap_threshold + 1e-6
    frac = (gap - gap_threshold) / (gap_ceiling - gap_threshold)
    if frac > 1:
        frac = 1.0
    return 1.0 - max_discount * frac


def distractor_bundle_for(
    case_id: str,
    expected_pair_ids: list[str],
    forbidden_pair_ids: list[str],
    candidates: list[str],
    secret: str,
    n: int,
) -> list[str]:
    """Build a deterministic distractor pool for one retrieval case.

    Distractors are drawn from ``candidates`` but never overlap with
    ``expected_pair_ids`` or ``forbidden_pair_ids``. The selection is
    hashed against ``(secret, case_id, candidate_id)`` so the same
    validator reproduces the same bundle on replay, while a different
    secret yields a different bundle.
    """
    if n <= 0 or not candidates:
        return []
    disallow = set(expected_pair_ids) | set(forbidden_pair_ids)
    scored = [
        (pid, _digest(f"{secret}|distractor|{case_id}|{pid}"))
        for pid in candidates
        if pid not in disallow
    ]
    scored.sort(key=lambda pair: pair[1])
    return [pid for pid, _hash in scored[:n]]


def normalise_prompt_for_canary_check(value: str) -> str:
    """Lower-case, strip punctuation, and collapse whitespace.

    Mirrors the Go implementation: letters and digits survive (lower-cased);
    whitespace runs collapse to a single space; everything else is removed.
    Used by :func:`ensure_paraphrase_changed` to confirm a paraphrase is
    genuinely different from its public twin.
    """
    buf: list[str] = []
    for ch in value:
        if ch.isalpha() or ch.isdigit():
            buf.append(ch.lower())
        elif ch.isspace() or unicodedata.category(ch).startswith("Z"):
            buf.append(" ")
    return " ".join("".join(buf).split())


def ensure_paraphrase_changed(original: str, paraphrased: str) -> None:
    """Raise :class:`CanaryIdenticalError` if the paraphrase is too close.

    Two prompts are considered identical if they normalise to the same
    token stream under :func:`normalise_prompt_for_canary_check` — that
    covers punctuation-only, casing-only, and whitespace-only "rewrites"
    that miners could trivially detect.
    """
    if normalise_prompt_for_canary_check(original) == normalise_prompt_for_canary_check(
        paraphrased
    ):
        raise CanaryIdenticalError(
            "paraphrase identical to original after normalisation; "
            "refusing to ship as canary"
        )


# A safety check: float math drifts subtly across implementations; if the
# gap_ceiling-vs-threshold fixup ever produces a NaN we want a clear failure
# rather than a silent zero discount.
def _check_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")

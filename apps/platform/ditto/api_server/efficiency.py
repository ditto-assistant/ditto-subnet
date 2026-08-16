"""Platform-side relative token-efficiency adjustment for bench_version >= 7.

Under the v7 quality-only contract the deterministic validator scores quality
and *records* audited token usage without ever letting it move the composite
(``formula_version = "v7-quality-only-v1"``). Efficiency incentives live here,
in the platform layer, per ``docs/relative-efficiency-bonus.md`` and the
validator-side spec (ditto-subnet ``docs/relative-efficiency-bonus-spec.md``):

* A **cohort** of the top-N quality-qualified agents per
  ``(bench_version, run_size)`` is frozen once per platform efficiency epoch.
  Near-identical lineages (same normalized-source hash, else same artifact
  sha256) collapse to their best entry before the reference is computed, so
  one lineage cannot define the frontier.
* The **reference** is robust: the efficient quartile (nearest-rank P25) of
  the cohort's audited chat-token totals — never the mean, an operator value,
  or the single minimum. It is frozen with the cohort for deterministic replay.
* Historical curves v1/v2 are strictly-upside bonuses. New Bench-v9 snapshots
  use curve v3: ``clamp((P25 / agent_cost) ** alpha, min, max)``. P25 is
  neutral, cheaper qualified agents receive a bounded bonus, and more expensive
  qualified agents receive a bounded penalty. The board's authoritative v9
  quality is adjusted only after all signed quality gates apply: canonical /
  continual quality normally, or full-confirmed quality while confirmation
  enforcement is active.
  Downside multiplies quality; upside closes only a bounded fraction of its
  remaining headroom, so imperfect quality never saturates at 1.0.
* **Frozen cohorts**: the snapshot (membership, floors, reference values) is
  computed once per epoch and persisted; a submission's bonus is assigned
  once, against the frozen reference of the epoch it finalized in, and never
  recomputed. Published historical scores never move.
* **Activation gate**: until a cohort has at least ``n_min`` deduped qualified
  members, the snapshot is inactive and no adjustment is assigned.

The deterministic validator scorer is NEVER touched by any of this: same
artifact, same validator score, forever. bench_version < 7 boards are
byte-identical to their pre-bonus behavior — no snapshot is ever computed
and no bonus row is ever written for them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from math import ceil, fsum, isfinite
from statistics import median
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.score_order import rank_submissions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.config import EfficiencyBonusConfig
    from ditto.db.models import EfficiencyBonus, EfficiencyCohortSnapshot
    from ditto.db.queries.scores import LedgerRow

logger = logging.getLogger(__name__)

# The relative bonus applies to the quality-only token contract only. v6 and
# earlier keep their validator-side absolute-budget behavior untouched.
MIN_BONUS_BENCH_VERSION = 7

# Ranked (emission-eligible) runs administer the full benchmark, so the
# leaderboard cohort is always the ``full`` generator profile. Smaller
# smoke/practice profiles are never ranked and never earn a bonus.
BONUS_RUN_SIZE = "full"

# Fraction of the previous epoch's cohort median memory_mean used as the
# next epoch's memory floor (spec: ``M_min = 0.8 x previous median``).
MEMORY_FLOOR_FRACTION = 0.8

# Fraction of the cohort used for the full-bonus frontier (efficient quartile).
FRONTIER_QUANTILE = 0.25

# Frozen bonus-curve policy versions. A snapshot records the version it was
# frozen under so ``reference_from_snapshot`` reproduces its bonuses exactly,
# forever, regardless of later curve changes.
CURVE_VERSION_SINGLE_TIER = 1
"""Original curve: full ``bonus_cap`` at or below P25, zero at or above the
median, linear in between."""
CURVE_VERSION_TWO_TIER = 2
"""Two-tier curve: tier 1 as above; tier 2 ramps ``bonus_cap`` up to
``deep_bonus_cap`` between P25 and ``deep_frontier_ratio x P25``, then
SATURATES flat below that deep frontier (racing toward zero tokens earns
nothing extra)."""
CURVE_VERSION_BOUNDED_FACTOR = 3
"""Bounded power curve for Bench v9+:
``clamp((reference_cost / agent_cost) ** alpha, minimum, maximum)``."""
BOUNDED_FACTOR_BENCH_VERSION = 9
"""The first bench version scored by curve v3. Later versions (v10+) inherit the
same bounded-factor tie-break: the signed v9 base-evidence contract carries
forward, stamped with the run's actual bench version. The legacy two-tier fold
never applies above this version."""
V9_TOKEN_COST_QUORUM = 3
"""Every accepted k=3 score row must carry valid v9 cost evidence."""

# Curve-v3's factor-specific model-use integrity floor.  The signed v9 base
# contract intentionally needs only one attributed case (one basis point),
# which is sufficient to keep a quality score non-zero but far too weak to
# award a low-cost bonus: a deterministic harness could make one token probe
# and bypass the model for the rest of the run.  These deliberately generous
# floors reuse the already-calibrated Platform model-use policy, with the
# additional benefit of v9's signature-bound distinct-case attribution.  They
# are part of curve version 3 (not runtime knobs), so the authority is stable.
V9_EFFICIENCY_MIN_MODEL_CASE_FRACTION_DENOMINATOR = 2
"""At least half of eligible cases must have attributed inference.

This is the smallest literal boundary at which the run was not *mostly*
model-bypassed. Typed v9 evidence also guarantees that each such distinct case
has at least one successful controlled request.
"""
V9_EFFICIENCY_MIN_PROMPT_TOKENS_PER_CASE = 200
V9_EFFICIENCY_MIN_PROMPT_TOKENS_PER_REQUEST = 300

EFFICIENCY_EVIDENCE_POLL_SECONDS = 0.0
"""Default to a fresh cheap watermark so new scores are assigned immediately.

The coordinator still single-flights concurrent callers and skips the expensive
ledger whenever that watermark is unchanged. A positive interval remains
available to deliberately trade assignment latency for fewer scalar reads.
"""


@dataclass(frozen=True)
class EfficiencyCandidate:
    """One finalized, ranked submission considered for a cohort."""

    agent_id: UUID
    miner_hotkey: str
    lineage_key: str
    composite: float
    memory_mean: float
    token_total: float | None
    """Mean audited chat-token cost across comparable accepted seeds.

    Legacy curves read complete relay ``token_usage`` accounting. Curve v3
    requires all three accepted quorum roots plus every protocol-19 continual
    retest that carries cost authority. Validator replicates are medianed per
    seed, then all evaluated seeds are averaged with equal weight. ``None``
    means the submission is unqualified for an adjustment.
    """
    first_seen: datetime
    owner_key: str | None = None
    """Canonical payment/attestation owner root. One owner contributes at
    most one candidate to the frozen reference, regardless of generations.
    ``None`` is a source-compatible unique-per-agent fallback for pure tests."""
    collapsed_agent_ids: tuple[UUID, ...] = ()
    """Qualified same-owner generations removed before lineage dedupe."""


@dataclass(frozen=True)
class CohortMember:
    """One deduped lineage entry of a frozen cohort."""

    agent_id: UUID
    miner_hotkey: str
    lineage_key: str
    composite: float
    memory_mean: float
    token_total: float
    collapsed_agent_ids: tuple[UUID, ...]
    """Other qualified agent ids that shared this lineage key and were
    collapsed into this (best-scoring) entry."""
    first_seen: datetime | None = None
    """Canonical score-order tiebreak captured with new snapshots.

    ``None`` exists only so snapshots frozen before this audit field was added
    remain readable. Newly built members always carry the winning candidate's
    timestamp.
    """


@dataclass(frozen=True)
class CohortReference:
    """The frozen per-epoch cohort snapshot: membership + robust reference."""

    bench_version: int
    run_size: str
    epoch_index: int
    active: bool
    cohort_limit: int
    n_min: int
    bonus_cap: float
    quality_floor: float
    memory_floor: float
    reference_p25_tokens: float | None
    reference_median_tokens: float | None
    members: tuple[CohortMember, ...]
    curve_version: int = CURVE_VERSION_SINGLE_TIER
    """Bonus-curve policy this snapshot was frozen under. Defaults to the
    single-tier legacy curve so rehydrated pre-tier snapshots (or hand-built
    references that omit the tier-2 knobs) reproduce their original bonuses."""
    deep_bonus_cap: float | None = None
    """Tier-2 saturation cap (two-tier curve only); ``>= bonus_cap``, <= 0.10."""
    deep_frontier_ratio: float | None = None
    """Deep frontier as a fraction of P25 (two-tier curve only), in (0, 1)."""
    factor_alpha: float | None = None
    """Power exponent frozen for the bounded-factor curve (v3 only)."""
    minimum_factor: float | None = None
    """Lower clamp frozen for the bounded-factor curve (v3 only)."""
    maximum_factor: float | None = None
    """Upper clamp frozen for the bounded-factor curve (v3 only)."""


def lineage_key(normalized_source_hash: str | None, sha256: str) -> str:
    """The best-effort lineage identity used to collapse near-identical
    submissions: the canonicalized-source hash when the platform computed one
    (survives repack/reformat), else the exact artifact digest. Prefixed so
    the two channels never collide."""
    if normalized_source_hash:
        return f"nsh:{normalized_source_hash}"
    return f"sha:{sha256}"


def audited_token_total(
    details_blobs: Sequence[Mapping[str, Any] | None],
) -> float | None:
    """Median audited chat token total across an agent's quorum score rows.

    Only relay-metered usage counts (``details.token_usage`` written by the
    validator from the trusted broker; miner-reported numbers never reach this
    blob). A row contributes only when its accounting is ``complete`` with no
    unavailable usage, so a partially metered run cannot lowball the total.
    Returns ``None`` when no row qualifies — such a submission is excluded
    from the cohort and can never earn a bonus (strictly upside: it simply
    keeps its unmodified composite).
    """
    totals: list[int] = []
    for details in details_blobs:
        if not isinstance(details, Mapping):
            continue
        usage = details.get("token_usage")
        if not isinstance(usage, Mapping):
            continue
        if usage.get("status") != "complete":
            continue
        unavailable = usage.get("usage_unavailable", 0)
        if isinstance(unavailable, bool) or not isinstance(unavailable, int):
            continue
        if unavailable != 0:
            continue
        total = usage.get("total_tokens")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            continue
        totals.append(total)
    if not totals:
        return None
    return float(median(totals))


def audited_v9_run_token_total(
    details: Mapping[str, Any] | None,
    *,
    base_evidence_sha256: str | None = None,
) -> int | None:
    """Return one v9 run's signature-bound, integrity-qualified token cost.

    Curve v3 deliberately does not read the legacy ``token_usage`` or
    ``platform_model_use_reconciliation`` annotations. Its integrity authority
    is the typed v9 base root whose digest is bound into the validator receipt.
    ``base_evidence_sha256`` is supplied directly while accepting a continual
    retest; persisted quorum rows carry the same value beside ``v9_base`` in
    ``details``. Only enforce-mode model-use passes that also clear curve-v3's
    factor-specific breadth and prompt-size integrity floors contribute.
    """
    from pydantic import ValidationError

    from ditto_screening_protocol.bench_v9 import (
        V9_SCORE_CONTRACT_MANIFEST_SHA256,
        V9_SCORE_CONTRACT_REVISION,
        V9BaseEvidence,
    )

    if not isinstance(details, Mapping):
        return None
    raw = details.get("v9_base")
    if not isinstance(raw, Mapping):
        return None
    try:
        evidence = V9BaseEvidence.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None
    # The score signature binds this sibling digest, not the opaque JSON
    # object directly. Score intake enforces the same identity; repeat it here
    # so malformed/imported history cannot turn an unsigned root into cost
    # authority.
    expected_digest = base_evidence_sha256 or details.get("base_evidence_sha256")
    if expected_digest != evidence.digest_hex():
        return None
    gates = evidence.score_gates
    model_use = gates.model_use
    if (
        evidence.score_contract.revision != V9_SCORE_CONTRACT_REVISION
        or evidence.score_contract.manifest_sha256 != V9_SCORE_CONTRACT_MANIFEST_SHA256
        or gates.threshold_profile.id != V9_SCORE_CONTRACT_REVISION
        or gates.threshold_profile.manifest_sha256 != V9_SCORE_CONTRACT_MANIFEST_SHA256
        or gates.rollout_mode != "enforce"
        or model_use.result != "passed"
        or model_use.factor_bps != 10_000
        or evidence.semantic_gate_factor_bps != 10_000
        or evidence.applied_gate_factor_bps != 10_000
        or model_use.successful_requests <= 0
    ):
        return None
    eligible_cases = model_use.eligible_cases
    if (
        eligible_cases <= 0
        or not model_use.case_attribution_complete
        or model_use.successful_inference_cases
        * V9_EFFICIENCY_MIN_MODEL_CASE_FRACTION_DENOMINATOR
        < eligible_cases
        or model_use.prompt_tokens
        < eligible_cases * V9_EFFICIENCY_MIN_PROMPT_TOKENS_PER_CASE
        or model_use.prompt_tokens
        < model_use.successful_requests * V9_EFFICIENCY_MIN_PROMPT_TOKENS_PER_REQUEST
    ):
        return None
    total = model_use.prompt_tokens + model_use.completion_tokens
    return total if total > 0 else None


def audited_v9_token_total(
    details_blobs: Sequence[Mapping[str, Any] | None],
    *,
    continual_seed_token_totals: Sequence[float] = (),
    continual_cost_evidence_valid: bool = True,
) -> float | None:
    """Mean v9 cost over the pinned quorum seed and accepted retest seeds.

    The three canonical roots are mandatory. Protocol-19 single-seed continual
    retests contribute one further observation per seed, so an agent the fleet
    keeps retesting is costed on its ongoing spend rather than on its first day
    alone. Legacy retests without the new cost binding are ignored; an
    explicitly invalid new retest fails the whole cost closed so model-bypassing
    work cannot disappear from the aggregate.

    A **seed** is the unit of observation, twice over. The k=3 quorum re-scores
    one pinned dataset seed, so its three receipts are validator replicates of a
    single observation, not three independent samples — they median into one
    entry exactly as the confirmation ledger medians each retest seed. The
    resulting per-seed observations are then averaged, giving every evaluated
    seed equal weight regardless of how many validators happened to score it.
    With no retests this reduces byte-for-byte to the frozen median-of-quorum
    behaviour, leaving agents the fleet has never retested completely
    unaffected by this path.
    """
    quorum = [audited_v9_run_token_total(details) for details in details_blobs]
    # Fail closed: a factor that can penalize peers must not be inferred from a
    # cherry-picked subset of the score quorum. All three accepted roots need
    # valid, enforced model-use evidence.
    if (
        len(details_blobs) != V9_TOKEN_COST_QUORUM
        or any(total is None for total in quorum)
        or not continual_cost_evidence_valid
    ):
        return None
    observations = [float(median([total for total in quorum if total is not None]))]
    for total in continual_seed_token_totals:
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not isfinite(total)
            or total <= 0
        ):
            return None
        observations.append(float(total))
    return fsum(observations) / len(observations)


def dedupe_lineages(
    candidates: Sequence[EfficiencyCandidate],
) -> list[CohortMember]:
    """Collapse candidates sharing a lineage key to one entry each.

    The surviving entry is the best-scoring one, picked with the canonical
    comparator (:func:`ditto.score_order.rank_submissions`):
    highest composite, then earliest ``first_seen``, then lowest ``agent_id``.
    Candidates without an audited token total never reach this function.
    """
    by_lineage: dict[str, list[EfficiencyCandidate]] = {}
    for candidate in candidates:
        by_lineage.setdefault(candidate.lineage_key, []).append(candidate)
    members: list[CohortMember] = []
    for key, group in by_lineage.items():
        ordered = rank_submissions(group)
        best = ordered[0]
        assert best.token_total is not None  # filtered upstream
        members.append(
            CohortMember(
                agent_id=best.agent_id,
                miner_hotkey=best.miner_hotkey,
                lineage_key=key,
                composite=best.composite,
                memory_mean=best.memory_mean,
                token_total=best.token_total,
                collapsed_agent_ids=tuple(
                    dict.fromkeys(
                        (
                            *best.collapsed_agent_ids,
                            *(c.agent_id for c in ordered[1:]),
                            *(
                                nested
                                for candidate in ordered[1:]
                                for nested in candidate.collapsed_agent_ids
                            ),
                        )
                    )
                ),
                first_seen=best.first_seen,
            )
        )

    # Every member created above came from an EfficiencyCandidate and has a real
    # first_seen. The optional type exists only for rehydrated legacy snapshots,
    # which never enter this ranking path. Keep the ordering itself in the one
    # canonical comparator shared by all ranking surfaces.
    assert all(member.first_seen is not None for member in members)
    return rank_submissions(members)  # type: ignore[type-var]


def dedupe_owners(
    candidates: Sequence[EfficiencyCandidate],
) -> list[EfficiencyCandidate]:
    """Choose one reference candidate per canonical emission owner.

    Reference construction cannot depend on the factor whose P25 it is about to
    derive. It therefore selects highest authoritative quality first. At equal
    quality, lower audited cost wins before arrival time—the exact tie policy
    curve v3 is meant to establish—and only then uses the canonical timestamp /
    UUID tie-break. Every eligible generation still receives an assignment
    after the reference freezes, so the official owner representative can later
    be selected by the curve-v3 downside/headroom score transform.
    """
    by_owner: dict[str, list[EfficiencyCandidate]] = {}
    for candidate in candidates:
        owner_key = candidate.owner_key or f"agent:{candidate.agent_id}"
        by_owner.setdefault(owner_key, []).append(candidate)
    selected: list[EfficiencyCandidate] = []
    for group in by_owner.values():
        ordered = sorted(
            group,
            key=lambda candidate: (
                -candidate.composite,
                (
                    candidate.token_total
                    if candidate.token_total is not None
                    else float("inf")
                ),
                candidate.first_seen,
                str(candidate.agent_id),
            ),
        )
        best = ordered[0]
        selected.append(
            replace(
                best,
                collapsed_agent_ids=tuple(
                    dict.fromkeys(
                        (
                            *best.collapsed_agent_ids,
                            *(candidate.agent_id for candidate in ordered[1:]),
                        )
                    )
                ),
            )
        )
    return selected


def nearest_rank_percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile over ``values`` (must be non-empty).

    ``rank = ceil(fraction * n)`` clamped to ``[1, n]`` — an actual observed
    value, never an interpolation, matching the validator-side baseline
    convention.
    """
    ordered = sorted(values)
    rank = min(len(ordered), max(1, ceil(fraction * len(ordered))))
    return float(ordered[rank - 1])


def qualifies(
    composite: float,
    memory_mean: float,
    token_total: float | None,
    *,
    quality_floor: float,
    memory_floor: float,
) -> bool:
    """The quality gate: complete audited usage AND both quality floors.

    The memory floor exists so a harness cannot buy efficiency by gutting the
    memory half of the benchmark; the composite floor blocks sandbagging into
    a cheap-but-bad run.
    """
    if token_total is None:
        return False
    return composite >= quality_floor and memory_mean >= memory_floor


def build_cohort_snapshot(
    candidates: Sequence[EfficiencyCandidate],
    *,
    bench_version: int,
    run_size: str,
    epoch_index: int,
    cohort_limit: int,
    n_min: int,
    bonus_cap: float,
    quality_floor: float,
    memory_floor: float,
    deep_bonus_cap: float | None = None,
    deep_frontier_ratio: float | None = None,
    curve_version: int | None = None,
    factor_alpha: float | None = None,
    minimum_factor: float | None = None,
    maximum_factor: float | None = None,
) -> CohortReference:
    """Freeze one epoch's cohort: qualify, dedupe, cap at top-N, derive the
    robust reference. Pure and deterministic — the same candidates always
    produce the same snapshot. With both tier-2 knobs supplied the snapshot
    is frozen under the two-tier curve (:data:`CURVE_VERSION_TWO_TIER`);
    otherwise under the single-tier legacy curve."""
    qualified = [
        candidate
        for candidate in candidates
        if qualifies(
            candidate.composite,
            candidate.memory_mean,
            candidate.token_total,
            quality_floor=quality_floor,
            memory_floor=memory_floor,
        )
    ]
    members = dedupe_lineages(dedupe_owners(qualified))[:cohort_limit]
    active = len(members) >= n_min
    p25: float | None = None
    med: float | None = None
    if active:
        totals = [member.token_total for member in members]
        p25 = nearest_rank_percentile(totals, FRONTIER_QUANTILE)
        med = float(median(totals))
    frozen_curve = curve_version
    if frozen_curve is None:
        frozen_curve = (
            CURVE_VERSION_TWO_TIER
            if deep_bonus_cap is not None and deep_frontier_ratio is not None
            else CURVE_VERSION_SINGLE_TIER
        )
    return CohortReference(
        bench_version=bench_version,
        run_size=run_size,
        epoch_index=epoch_index,
        active=active,
        cohort_limit=cohort_limit,
        n_min=n_min,
        bonus_cap=bonus_cap,
        quality_floor=quality_floor,
        memory_floor=memory_floor,
        reference_p25_tokens=p25,
        reference_median_tokens=med,
        members=tuple(members),
        curve_version=frozen_curve,
        deep_bonus_cap=(
            deep_bonus_cap if frozen_curve == CURVE_VERSION_TWO_TIER else None
        ),
        deep_frontier_ratio=(
            deep_frontier_ratio if frozen_curve == CURVE_VERSION_TWO_TIER else None
        ),
        factor_alpha=(
            factor_alpha if frozen_curve == CURVE_VERSION_BOUNDED_FACTOR else None
        ),
        minimum_factor=(
            minimum_factor if frozen_curve == CURVE_VERSION_BOUNDED_FACTOR else None
        ),
        maximum_factor=(
            maximum_factor if frozen_curve == CURVE_VERSION_BOUNDED_FACTOR else None
        ),
    )


def bounded_efficiency_factor(
    agent_cost: float,
    *,
    reference_cost: float,
    alpha: float,
    minimum_factor: float,
    maximum_factor: float,
) -> float:
    """Apply the frozen v3 power curve and clamp its multiplier.

    The reference is neutral by construction. Lower audited cost increases the
    multiplier and higher audited cost decreases it, but neither direction can
    escape the snapshot's frozen [minimum, maximum] envelope.
    """
    values = (agent_cost, reference_cost, alpha, minimum_factor, maximum_factor)
    if any(not isfinite(value) for value in values):
        return 1.0
    if (
        agent_cost <= 0.0
        or reference_cost <= 0.0
        or not 0.0 < alpha <= 1.0
        or not 0.85 <= minimum_factor <= 1.0
        or not 1.0 <= maximum_factor <= 1.10
    ):
        return 1.0
    raw = (reference_cost / agent_cost) ** alpha
    return min(max(raw, minimum_factor), maximum_factor)


def factor_for_submission(
    composite: float,
    memory_mean: float,
    token_total: float | None,
    reference: CohortReference,
) -> float | None:
    """One curve-v3 assignment, or ``None`` when cost integrity is absent.

    Quality and memory floors choose the robust reference cohort and gate v3
    upside. They must not exempt an otherwise valid submission from downside:
    otherwise an expensive agent could sandbag just below a floor to avoid a
    penalty, and improving quality across that boundary could lower its final
    score. A below-floor submission therefore receives ``min(raw, 1)``—never a
    bonus, but the same bounded penalty as an above-floor peer.
    """
    if (
        reference.curve_version != CURVE_VERSION_BOUNDED_FACTOR
        or reference.bench_version < BOUNDED_FACTOR_BENCH_VERSION
    ):
        return None
    if not reference.active or token_total is None:
        return None
    if (
        reference.reference_p25_tokens is None
        or reference.factor_alpha is None
        or reference.minimum_factor is None
        or reference.maximum_factor is None
    ):
        return None
    factor = bounded_efficiency_factor(
        token_total,
        reference_cost=reference.reference_p25_tokens,
        alpha=reference.factor_alpha,
        minimum_factor=reference.minimum_factor,
        maximum_factor=reference.maximum_factor,
    )
    if not qualifies(
        composite,
        memory_mean,
        token_total,
        quality_floor=reference.quality_floor,
        memory_floor=reference.memory_floor,
    ):
        return min(factor, 1.0)
    return factor


def bonus_fraction(
    token_total: float,
    *,
    reference_p25: float,
    reference_median: float,
    cap: float,
    deep_cap: float | None = None,
    deep_frontier_ratio: float | None = None,
) -> float:
    """The bonus curve — continuous and monotone non-increasing in usage.

    Tier 1 (always): zero at or above the cohort median, linear up to ``cap``
    at the efficient quartile P25. The zero point deliberately sits at the
    *median* (the operator's cohort-relative rule) rather than the spec
    draft's absolute ``4 x P25`` multiple: both are robust, but the median
    anchors the taper to the cohort's actual dispersion, so a cohort of
    uniformly lean harnesses does not hand near-full bonuses to its own
    laggards. Degenerate cohorts (median == P25) collapse to a step at the
    frontier.

    Tier 2 (only when both ``deep_cap`` and ``deep_frontier_ratio`` are
    given — the two-tier policy): between P25 and the deep frontier
    ``deep_frontier_ratio x P25`` the bonus keeps climbing linearly from
    ``cap`` to ``deep_cap``; both tiers equal ``cap`` exactly at P25, so the
    curve is continuous there. Below the deep frontier the bonus SATURATES
    flat at ``deep_cap``: deliberately no extra reward for racing toward
    zero tokens, which would otherwise incentivize gutting real work the
    quality gate cannot fully observe. All anchors remain pure cohort
    statistics (P25, median, a fraction of P25) — never the single cheapest
    submission.
    """
    if token_total <= reference_p25:
        if deep_cap is None or deep_frontier_ratio is None:
            return cap  # single-tier legacy policy
        deep_frontier = deep_frontier_ratio * reference_p25
        if token_total <= deep_frontier:
            return deep_cap
        span = reference_p25 - deep_frontier
        if span <= 0.0:
            return deep_cap  # degenerate (P25 == 0): step at the frontier
        return cap + (deep_cap - cap) * (reference_p25 - token_total) / span
    if token_total >= reference_median:
        return 0.0
    span = reference_median - reference_p25
    if span <= 0.0:
        return 0.0
    return cap * (reference_median - token_total) / span


def bonus_for_submission(
    composite: float,
    memory_mean: float,
    token_total: float | None,
    reference: CohortReference,
) -> float:
    """A submission's frozen bonus against its epoch's cohort reference.

    Zero unless the snapshot is active, the submission carries complete
    audited usage, and it clears the frozen quality gate. Never negative:
    an expensive or unqualified run keeps its unmodified composite.
    """
    if not reference.active:
        return 0.0
    if reference.reference_p25_tokens is None:
        return 0.0
    if reference.reference_median_tokens is None:
        return 0.0
    if token_total is None:
        return 0.0
    if not qualifies(
        composite,
        memory_mean,
        token_total,
        quality_floor=reference.quality_floor,
        memory_floor=reference.memory_floor,
    ):
        return 0.0
    two_tier = reference.curve_version == CURVE_VERSION_TWO_TIER
    return bonus_fraction(
        token_total,
        reference_p25=reference.reference_p25_tokens,
        reference_median=reference.reference_median_tokens,
        cap=reference.bonus_cap,
        deep_cap=reference.deep_bonus_cap if two_tier else None,
        deep_frontier_ratio=reference.deep_frontier_ratio if two_tier else None,
    )


def effective_composite(composite: float, bonus: float) -> float:
    """The legacy v1/v2 ranking score: ``composite * (1 + bonus)``.

    Multiplicative, matching the spec's ``bonus_multiplier`` form: the fold
    compares composites, so a scale factor preserves ordering semantics and a
    zero composite can never buy weight from cheapness alone. Bounded by
    ``1 + cap`` (<= 1.10). Curve v3 does not use this helper: it multiplies
    downside and applies upside only to remaining quality headroom. The
    validator's composite is never modified."""
    return composite * (1.0 + bonus)


def epoch_index_for(now: datetime, epoch_hours: int) -> int:
    """The platform efficiency epoch a wall-clock instant falls in.

    Fixed UTC windows of ``epoch_hours`` since the Unix epoch. The platform
    has no persisted scoring-epoch clock of its own (chain tempos live in the
    validator fold), so the bonus defines its own coarse, deterministic
    window; default 24 h — slow enough that a cohort is a stable population,
    fast enough that new lean harnesses move the reference within a day.
    """
    return int(now.timestamp()) // (epoch_hours * 3600)


def floors_from_previous(
    previous_members: Sequence[Mapping[str, Any]] | None,
    *,
    quality_floor: float,
    memory_floor: float,
    previous_curve_version: int | None = None,
    current_curve_version: int | None = None,
) -> tuple[float, float]:
    """Derive this epoch's quality floors from the previous ACTIVE cohort.

    Spec policy: ``Q_min`` = previous cohort's median composite, ``M_min`` =
    ``MEMORY_FLOOR_FRACTION x`` previous median memory_mean — both never
    below the configured static floors, which alone govern the first epoch.

    Curve v3 ranks independently confirmed full-v9 quality, whereas legacy
    curves stored the earlier base composite. Those values are not comparable:
    on the v2 -> v3 transition, configured floors govern until an active v3
    cohort exists. Subsequent v3 epochs may inherit only from a v3 snapshot.
    """
    if (
        current_curve_version == CURVE_VERSION_BOUNDED_FACTOR
        and previous_curve_version != CURVE_VERSION_BOUNDED_FACTOR
    ):
        return quality_floor, memory_floor
    if not previous_members:
        return quality_floor, memory_floor
    composites = [
        float(member["composite"])
        for member in previous_members
        if isinstance(member.get("composite"), (int, float))
    ]
    memory_means = [
        float(member["memory_mean"])
        for member in previous_members
        if isinstance(member.get("memory_mean"), (int, float))
    ]
    derived_quality = (
        max(quality_floor, median(composites)) if composites else (quality_floor)
    )
    derived_memory = (
        max(memory_floor, MEMORY_FLOOR_FRACTION * median(memory_means))
        if memory_means
        else memory_floor
    )
    return derived_quality, derived_memory


# ---------------------------------------------------------------------------
# Orchestration (DB-backed): materialize + read the frozen state.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EfficiencyBoardView:
    """What a board read needs: the governing snapshot + frozen bonus rows."""

    snapshot: EfficiencyCohortSnapshot | None
    bonuses: dict[UUID, EfficiencyBonus]
    preview: bool = False
    """True when nothing here is persisted or applied.

    A preview is computed at read time from live state and thrown away. Its
    numbers answer "what WOULD the bonus be", never "what IS an agent's score".
    Callers must render it as inactive and must never fold it into a composite.
    """
    preview_reference: CohortReference | None = None
    """The in-memory cohort a preview was computed against; ``None`` when this
    is a real read of frozen rows."""
    preview_bonuses: dict[UUID, float] | None = None
    """Per-agent bonus fractions a preview computed, keyed by agent."""
    preview_factors: dict[UUID, float] | None = None
    """Per-agent bounded factors a curve-v3 preview computed, keyed by agent."""
    preview_candidate_count: int | None = None
    """Finalized ranked rows considered by a non-persisting preview."""
    preview_cost_evidence_count: int | None = None
    """Preview candidates with complete audited cost evidence."""
    preview_quality_qualified_count: int | None = None
    """Costed preview candidates that also clear the frozen quality floors."""
    preview_owner_deduped_count: int | None = None
    """Quality-qualified preview candidates after payment-owner dedupe."""
    preview_lineage_deduped_count: int | None = None
    """Quality-qualified preview candidates after owner and lineage dedupe."""


@dataclass(frozen=True)
class EfficiencyEvidenceWatermark:
    """Cheap identity of every mutable input to efficiency materialization."""

    active_bench_version: int
    candidate_digest: str
    owner_attestation_digest: str
    score_count: int
    score_updated_at: datetime | None
    confirmation_score_count: int
    confirmation_score_created_at: datetime | None
    confirmation_subject_count: int
    confirmation_subject_updated_at: datetime | None
    confirmation_bundle_count: int
    confirmation_bundle_updated_at: datetime | None
    confirmation_settings_revision: int


def _rows_digest(rows: Sequence[Any]) -> str:
    """Return a stable digest for a small ordered scalar projection."""

    digest = hashlib.sha256()
    for row in rows:
        payload = repr(tuple(row)).encode()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


async def _efficiency_evidence_watermark(
    session: AsyncSession,
) -> EfficiencyEvidenceWatermark:
    """Read the small indexed state that can change a materialization result.

    This deliberately avoids :func:`list_eligible_ledger`. Candidate identity
    is a scalar-only projection and the score/evidence ledgers collapse to
    counts plus monotonic timestamps. The production query is a few
    milliseconds instead of a full Python ranking-ledger rebuild.
    """
    from sqlalchemy import func, select

    from ditto.api_models.agent_status import AgentStatus
    from ditto.db.models import (
        Agent,
        ConfirmationBundle,
        ConfirmationBundleSettingsRevision,
        ConfirmationBundleSubject,
        ConfirmationScore,
        EvaluationPayment,
        OwnerAttestation,
        Score,
    )
    from ditto.db.queries.benchmark_rollout import active_bench_version

    bench_version = await active_bench_version(session)
    candidate_rows = (
        await session.execute(
            select(
                Agent.agent_id,
                Agent.status,
                Agent.sha256,
                Agent.normalized_source_hash,
                Agent.created_at,
                Agent.miner_hotkey,
                EvaluationPayment.miner_coldkey,
            )
            .select_from(Agent)
            .join(
                Score,
                (Score.agent_id == Agent.agent_id)
                & (Score.bench_version == bench_version),
            )
            .outerjoin(
                EvaluationPayment,
                EvaluationPayment.agent_id == Agent.agent_id,
            )
            .where(Agent.status == AgentStatus.SCORED)
            .distinct()
            .order_by(Agent.agent_id)
        )
    ).all()
    owner_attestation_rows = (
        await session.execute(
            select(
                OwnerAttestation.netuid,
                OwnerAttestation.hotkey_lo,
                OwnerAttestation.hotkey_hi,
                OwnerAttestation.created_at,
            )
            .where(OwnerAttestation.revoked_at.is_(None))
            .order_by(
                OwnerAttestation.netuid,
                OwnerAttestation.hotkey_lo,
                OwnerAttestation.hotkey_hi,
            )
        )
    ).all()

    def count_for(model: Any) -> Any:
        return (
            select(func.count())
            .select_from(model)
            .where(model.bench_version == bench_version)
            .scalar_subquery()
        )

    def max_for(model: Any, column: Any) -> Any:
        return (
            select(func.max(column))
            .select_from(model)
            .where(model.bench_version == bench_version)
            .scalar_subquery()
        )

    state = (
        await session.execute(
            select(
                count_for(Score),
                max_for(Score, Score.updated_at),
                count_for(ConfirmationScore),
                max_for(ConfirmationScore, ConfirmationScore.created_at),
                count_for(ConfirmationBundleSubject),
                max_for(
                    ConfirmationBundleSubject,
                    ConfirmationBundleSubject.updated_at,
                ),
                count_for(ConfirmationBundle),
                max_for(ConfirmationBundle, ConfirmationBundle.updated_at),
                select(
                    func.coalesce(
                        func.max(ConfirmationBundleSettingsRevision.revision), 0
                    )
                )
                .where(ConfirmationBundleSettingsRevision.scope == "*")
                .scalar_subquery(),
            )
        )
    ).one()
    return EfficiencyEvidenceWatermark(
        active_bench_version=bench_version,
        candidate_digest=_rows_digest(candidate_rows),
        owner_attestation_digest=_rows_digest(owner_attestation_rows),
        score_count=int(state[0]),
        score_updated_at=state[1],
        confirmation_score_count=int(state[2]),
        confirmation_score_created_at=state[3],
        confirmation_subject_count=int(state[4]),
        confirmation_subject_updated_at=state[5],
        confirmation_bundle_count=int(state[6]),
        confirmation_bundle_updated_at=state[7],
        confirmation_settings_revision=int(state[8]),
    )


class EfficiencyStateMaterializer:
    """Single-flight, evidence-driven efficiency materialization coordinator.

    A successful materialization is reusable until its epoch, effective config,
    or evidence watermark changes. Failures are never cached. One instance per
    API process prevents validator and dashboard requests from rebuilding the
    same unchanged ledger.
    """

    def __init__(
        self, *, poll_seconds: float = EFFICIENCY_EVIDENCE_POLL_SECONDS
    ) -> None:
        self._poll_seconds = max(0.0, poll_seconds)
        self._lock = asyncio.Lock()
        self._last_scope: tuple[EfficiencyBonusConfig, int] | None = None
        self._last_watermark: EfficiencyEvidenceWatermark | None = None
        self._last_checked_at = 0.0

    async def ensure(
        self,
        session: AsyncSession,
        config: EfficiencyBonusConfig,
        *,
        now: datetime,
    ) -> None:
        if not config.enabled:
            return
        scope = (config, epoch_index_for(now, config.epoch_hours))
        checked_at = time.monotonic()
        if (
            self._last_scope == scope
            and self._last_watermark is not None
            and checked_at - self._last_checked_at < self._poll_seconds
        ):
            return

        async with self._lock:
            checked_at = time.monotonic()
            if (
                self._last_scope == scope
                and self._last_watermark is not None
                and checked_at - self._last_checked_at < self._poll_seconds
            ):
                return
            async with session.begin():
                watermark = await _efficiency_evidence_watermark(session)
            if self._last_scope == scope and self._last_watermark == watermark:
                self._last_checked_at = time.monotonic()
                return

            await ensure_efficiency_state(session, config, now=now)
            self._last_scope = scope
            self._last_watermark = watermark
            self._last_checked_at = time.monotonic()


async def ensure_current_efficiency_state(
    app_state: Any,
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    now: datetime,
) -> None:
    """Use the app coordinator, with a direct-call fallback for small tests."""

    materializer = getattr(app_state, "efficiency_materializer", None)
    if materializer is None:
        await ensure_efficiency_state(session, config, now=now)
        return
    await materializer.ensure(session, config, now=now)


def _candidates_from_rows(
    rows: Sequence[LedgerRow],
    token_totals: Mapping[UUID, float | None],
    *,
    curve_version: int = CURVE_VERSION_TWO_TIER,
) -> list[EfficiencyCandidate]:
    def quality_composite(row: LedgerRow) -> float | None:
        if curve_version != CURVE_VERSION_BOUNDED_FACTOR:
            return row.composite
        # ``list_eligible_ledger`` already resolves this contract boundary in
        # one place. With confirmation enforcement off, ``official_composite``
        # is the canonical/continual v9 score. With enforcement on, the ledger
        # admits only full-confirmed rows and this is their verified full score.
        # Requiring ``row.v9_confirmation`` here as well made an active,
        # finalized base-only v9 board produce a zero-member efficiency cohort.
        quality = getattr(row, "official_composite", row.composite)
        if (
            row.bench_version >= BOUNDED_FACTOR_BENCH_VERSION
            and isinstance(quality, (int, float))
            and not isinstance(quality, bool)
            and isfinite(quality)
            and 0.0 <= quality <= 1.0
        ):
            return float(quality)
        return None

    candidates: list[EfficiencyCandidate] = []
    for row in rows:
        quality = quality_composite(row)
        candidates.append(
            EfficiencyCandidate(
                agent_id=row.agent_id,
                miner_hotkey=row.miner_hotkey,
                lineage_key=lineage_key(row.normalized_source_hash, row.sha256),
                owner_key=(
                    getattr(row, "emission_owner_root", None) or f"agent:{row.agent_id}"
                ),
                composite=quality if quality is not None else 0.0,
                memory_mean=row.memory_mean,
                token_total=(
                    token_totals.get(row.agent_id) if quality is not None else None
                ),
                first_seen=row.first_seen,
            )
        )
    return candidates


async def _finalized_ranked_rows(session: AsyncSession) -> list[LedgerRow]:
    """The authoritative board's finalized (quorum), ranked rows."""
    from ditto.db.queries.scores import (
        SCORING_QUORUM,
        get_score_counts,
        list_eligible_ledger,
    )

    # Cohort lineage dedupe needs ``normalized_source_hash``. It catches the
    # same source repacked into byte-different tarballs; falling back to artifact
    # SHA alone lets those copies occupy several top-N slots and move P25. The
    # ledger API currently groups this scalar with the larger anti-copy sketch,
    # so load that projection here for reference integrity while still omitting
    # the much larger score-details JSON.
    rows = await list_eligible_ledger(
        session,
        include_fingerprints=True,
        include_details=False,
        dedupe_owners=False,
    )
    ranked = [row for row in rows if row.eligible]
    counts = await get_score_counts(
        session,
        [row.agent_id for row in ranked],
        bench_versions={row.agent_id: row.bench_version for row in ranked},
    )
    return [row for row in ranked if counts.get(row.agent_id, 0) >= SCORING_QUORUM]


async def ensure_efficiency_state(
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    now: datetime,
) -> None:
    """Materialize the current epoch's snapshot and any missing bonus rows.

    Idempotent and epoch-frozen: the first call inside an epoch computes and
    persists the cohort snapshot; every later call reuses it byte-for-byte.
    A finalized submission gets its bonus row inserted exactly once — against
    the frozen snapshot of the epoch this call runs in — and the row is never
    updated afterwards, so published scores never drift. Bonus rows (including
    explicit zeros) are only written while a snapshot is ACTIVE; before
    activation nothing is assigned, so the first active epoch freezes everyone
    present at that point.

    No-ops entirely when disabled or while the authoritative board is below
    ``bench_version`` 7, keeping v6-and-earlier behavior byte-identical.

    Opens its own transaction, so call it on a session with no transaction in
    progress (i.e. before the request's other reads). Concurrent
    materializers race safely: a loser of the unique-snapshot or bonus-row
    insert retries once and reuses the winner's frozen rows.
    """
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    if not config.enabled:
        return
    try:
        async with session.begin():
            await _materialize_epoch(session, config, now=now)
    except SAIntegrityError:
        # Another materializer froze the snapshot and/or some bonus rows
        # first. The frozen rows are canonical; one retry picks them up and
        # fills only the still-missing pieces.
        async with session.begin():
            await _materialize_epoch(session, config, now=now)


async def _materialize_epoch(
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    now: datetime,
) -> None:
    """One materialization pass; must run inside an open transaction."""
    from ditto.db.queries.efficiency import (
        get_bonus_rows,
        get_snapshot,
        insert_bonus,
        insert_snapshot,
        latest_snapshot,
        promote_v3_compatibility_placeholder,
    )
    from ditto.db.queries.scores import quorum_score_rows

    rows = await _finalized_ranked_rows(session)
    rows = [row for row in rows if row.bench_version >= MIN_BONUS_BENCH_VERSION]
    if not rows:
        return
    bench_version = max(row.bench_version for row in rows)
    rows = [row for row in rows if row.bench_version == bench_version]
    epoch = epoch_index_for(now, config.epoch_hours)

    agent_ids = [row.agent_id for row in rows]
    versions = dict.fromkeys(agent_ids, bench_version)
    snapshot = await get_snapshot(
        session,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        epoch_index=epoch,
    )
    curve_version = (
        snapshot.curve_version
        if snapshot is not None
        else (
            CURVE_VERSION_BOUNDED_FACTOR
            if bench_version >= BOUNDED_FACTOR_BENCH_VERSION
            else CURVE_VERSION_TWO_TIER
        )
    )
    existing = {}
    if snapshot is not None:
        # A frozen inactive snapshot can never activate later in its epoch.
        # More importantly, an active snapshot normally already has an
        # immutable assignment for every finalized row. Check that narrow,
        # indexed ledger before loading and decoding the quorum's score-detail
        # JSON. Validator job polling calls this materializer frequently; doing
        # the expensive audit again for a complete epoch was pure CPU churn.
        if not snapshot.active:
            return
        existing = await get_bonus_rows(
            session, agent_ids, bench_versions=versions, epoch_index=epoch
        )
        rows = [
            row
            for row in rows
            if row.agent_id not in existing
            or (
                curve_version == CURVE_VERSION_BOUNDED_FACTOR
                and existing[row.agent_id].factor is None
            )
        ]
        if not rows:
            return
        agent_ids = [row.agent_id for row in rows]
        versions = dict.fromkeys(agent_ids, bench_version)

    score_rows = await quorum_score_rows(session, agent_ids, bench_versions=versions)
    if curve_version == CURVE_VERSION_BOUNDED_FACTOR:
        from ditto.db.queries.confirmation_scores import (
            ConfirmationEfficiencyCosts,
            confirmation_efficiency_costs_by_agent,
        )

        continual_costs = await confirmation_efficiency_costs_by_agent(
            session, agent_ids=agent_ids, bench_version=bench_version
        )
        token_totals = {}
        for agent_id in agent_ids:
            retests = continual_costs.get(agent_id, ConfirmationEfficiencyCosts())
            token_totals[agent_id] = audited_v9_token_total(
                [score.details for score in score_rows.get(agent_id, [])],
                continual_seed_token_totals=retests.seed_token_totals,
                continual_cost_evidence_valid=retests.evidence_valid,
            )
    else:
        token_totals = {
            agent_id: audited_token_total(
                [score.details for score in score_rows.get(agent_id, [])]
            )
            for agent_id in agent_ids
        }
    candidates = _candidates_from_rows(rows, token_totals, curve_version=curve_version)

    if snapshot is None:
        previous = await latest_snapshot(
            session,
            bench_version=bench_version,
            run_size=BONUS_RUN_SIZE,
            max_epoch_index=epoch - 1,
            active_only=True,
        )
        quality_floor, memory_floor = floors_from_previous(
            previous.members if previous is not None else None,
            quality_floor=config.quality_floor,
            memory_floor=config.memory_floor,
            previous_curve_version=(
                previous.curve_version if previous is not None else None
            ),
            current_curve_version=curve_version,
        )
        reference = build_cohort_snapshot(
            candidates,
            bench_version=bench_version,
            run_size=BONUS_RUN_SIZE,
            epoch_index=epoch,
            cohort_limit=config.cohort_size,
            n_min=config.min_cohort,
            bonus_cap=config.cap,
            quality_floor=quality_floor,
            memory_floor=memory_floor,
            deep_bonus_cap=(
                config.deep_cap if curve_version == CURVE_VERSION_TWO_TIER else None
            ),
            deep_frontier_ratio=(
                config.deep_frontier_ratio
                if curve_version == CURVE_VERSION_TWO_TIER
                else None
            ),
            curve_version=curve_version,
            factor_alpha=(
                config.factor_alpha
                if curve_version == CURVE_VERSION_BOUNDED_FACTOR
                else None
            ),
            minimum_factor=(
                config.minimum_factor
                if curve_version == CURVE_VERSION_BOUNDED_FACTOR
                else None
            ),
            maximum_factor=(
                config.maximum_factor
                if curve_version == CURVE_VERSION_BOUNDED_FACTOR
                else None
            ),
        )
        snapshot = await insert_snapshot(session, reference)
        logger.info(
            "efficiency cohort frozen: bench_version=%d epoch=%d active=%s members=%d",
            bench_version,
            epoch,
            snapshot.active,
            len(snapshot.members or []),
        )
    if not snapshot.active:
        return

    reference = reference_from_snapshot(snapshot)
    # Scoped to THIS epoch. Without the epoch predicate an agent assigned a
    # bonus in any earlier epoch was skipped forever, which froze its bonus for
    # the life of the bench version -- the defect this key widening fixes. Rows
    # from earlier epochs are still present and still immutable; they simply no
    # longer suppress the current epoch's recomputation.
    if not existing:
        existing = await get_bonus_rows(
            session, agent_ids, bench_versions=versions, epoch_index=epoch
        )
    for candidate in candidates:
        factor = factor_for_submission(
            candidate.composite,
            candidate.memory_mean,
            candidate.token_total,
            reference,
        )
        existing_row = existing.get(candidate.agent_id)
        if existing_row is not None:
            if (
                reference.curve_version == CURVE_VERSION_BOUNDED_FACTOR
                and existing_row.factor is None
                and factor is not None
                and candidate.token_total is not None
            ):
                await promote_v3_compatibility_placeholder(
                    session,
                    existing_row,
                    token_total=candidate.token_total,
                    factor=factor,
                )
            continue
        bonus = (
            0.0
            if reference.curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else bonus_for_submission(
                candidate.composite,
                candidate.memory_mean,
                candidate.token_total,
                reference,
            )
        )
        # A v3 assignment is authority only when all signed cost-integrity
        # evidence is present. Do not freeze a null row: complete evidence
        # arriving later in this same epoch must still be able to qualify
        # against the already-frozen reference.
        if reference.curve_version == CURVE_VERSION_BOUNDED_FACTOR and factor is None:
            continue
        await insert_bonus(
            session,
            agent_id=candidate.agent_id,
            bench_version=bench_version,
            epoch_index=epoch,
            snapshot_id=snapshot.snapshot_id,
            token_total=candidate.token_total,
            bonus=bonus,
            factor=factor,
        )


def reference_from_snapshot(snapshot: EfficiencyCohortSnapshot) -> CohortReference:
    """Rehydrate the pure reference from a persisted snapshot row, so a bonus
    is always reproducible from stored data alone."""
    members: list[CohortMember] = []
    for raw in snapshot.members or []:
        if not isinstance(raw, Mapping):
            continue
        raw_first_seen = raw.get("first_seen")
        member_first_seen = (
            raw_first_seen
            if isinstance(raw_first_seen, datetime)
            else (
                datetime.fromisoformat(str(raw_first_seen))
                if raw_first_seen is not None
                else None
            )
        )
        members.append(
            CohortMember(
                agent_id=UUID(str(raw["agent_id"])),
                miner_hotkey=str(raw["miner_hotkey"]),
                lineage_key=str(raw["lineage_key"]),
                composite=float(raw["composite"]),
                memory_mean=float(raw["memory_mean"]),
                token_total=float(raw["token_total"]),
                collapsed_agent_ids=tuple(
                    UUID(str(value)) for value in raw.get("collapsed_agent_ids", [])
                ),
                first_seen=member_first_seen,
            )
        )
    return CohortReference(
        bench_version=snapshot.bench_version,
        run_size=snapshot.run_size,
        epoch_index=snapshot.epoch_index,
        active=snapshot.active,
        cohort_limit=snapshot.cohort_limit,
        n_min=snapshot.n_min,
        bonus_cap=snapshot.bonus_cap,
        quality_floor=snapshot.quality_floor,
        memory_floor=snapshot.memory_floor,
        reference_p25_tokens=snapshot.reference_p25_tokens,
        reference_median_tokens=snapshot.reference_median_tokens,
        members=tuple(members),
        # The frozen policy version travels with the snapshot: a pre-tier
        # (curve_version 1) snapshot reproduces its single-tier bonuses
        # exactly, even under a build whose config defaults to two tiers.
        curve_version=snapshot.curve_version,
        deep_bonus_cap=snapshot.deep_bonus_cap,
        deep_frontier_ratio=snapshot.deep_frontier_ratio,
        factor_alpha=snapshot.factor_alpha,
        minimum_factor=snapshot.minimum_factor,
        maximum_factor=snapshot.maximum_factor,
    )


async def read_efficiency_board(
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    bench_version: int,
    agent_ids: Sequence[UUID],
    bench_versions: Mapping[UUID, int],
    now: datetime,
    historical: bool = False,
) -> EfficiencyBoardView | None:
    """Read the snapshot and assignments governing one board render.

    Live/current boards always read assignments for the exact current epoch.
    Missing current rows therefore fail closed instead of silently inheriting
    an older adjustment. A settled historical board instead reproduces the
    assignments belonging to the selected latest snapshot for that retired
    benchmark. ``None`` means the policy cannot apply to this board at all.
    """
    from ditto.db.queries.efficiency import get_bonus_rows, latest_snapshot

    if not config.enabled or bench_version < MIN_BONUS_BENCH_VERSION:
        return None
    current_epoch = epoch_index_for(now, config.epoch_hours)
    snapshot = await latest_snapshot(
        session,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        max_epoch_index=current_epoch,
        active_only=False,
    )
    assignment_epoch = (
        snapshot.epoch_index if historical and snapshot is not None else current_epoch
    )
    bonuses = (
        await get_bonus_rows(
            session,
            agent_ids,
            bench_versions=bench_versions,
            epoch_index=assignment_epoch,
        )
        if not historical or snapshot is not None
        else {}
    )
    return EfficiencyBoardView(snapshot=snapshot, bonuses=bonuses)


async def preview_efficiency_board(
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    bench_version: int,
    now: datetime,
) -> EfficiencyBoardView | None:
    """Compute what the bonus WOULD be, persisting nothing.

    "I need to see the token boost whether or not it's active." Today the whole
    ``efficiency`` block disappears from the leaderboard when the bonus is off,
    so the feature is all-or-nothing: invisible, or live and moving composites.

    The obvious workaround -- ``enabled=True, fold_enabled=False`` -- is NOT
    read-only and must not be recommended. ``enabled`` is what gates
    :func:`ensure_efficiency_state`, which **writes**: it freezes a cohort
    snapshot and then inserts a bonus row per agent. Turning it on to look at
    the numbers is what froze rows at 04:43Z part-way through the token-budget
    transition.

    So this recomputes instead. Every input is already available -- finalized
    ranked rows and audited ``token_usage`` from the quorum's score details --
    and the entire bonus calculation
    (:func:`build_cohort_snapshot` -> :func:`bonus_for_submission`) is pure. The
    only thing dropped is the two ``INSERT``s. That makes genuine shadow
    observation possible AND removes the freeze hazard, which is strictly better
    than the enabled-without-fold path it replaces.

    One deliberate inexactness: the derived quality/memory floors normally come
    from the *previous active epoch's* frozen members, and when the bonus has
    never run there is no such snapshot. The preview then falls back to the
    static configured floors, exactly as a cold start would. It is a preview of
    what enabling would produce, not a promise of byte-identical numbers.

    Returns ``None`` when the bonus could not apply to this board at all, so an
    impossible preview is never dressed up as a zero one.
    """
    from ditto.db.queries.efficiency import latest_snapshot
    from ditto.db.queries.scores import quorum_score_rows

    if bench_version < MIN_BONUS_BENCH_VERSION:
        return None
    rows = [
        row
        for row in await _finalized_ranked_rows(session)
        if row.bench_version == bench_version
    ]
    if not rows:
        return None
    agent_ids = [row.agent_id for row in rows]
    versions = dict.fromkeys(agent_ids, bench_version)
    score_rows = await quorum_score_rows(session, agent_ids, bench_versions=versions)
    curve_version = (
        CURVE_VERSION_BOUNDED_FACTOR
        if bench_version >= BOUNDED_FACTOR_BENCH_VERSION
        else CURVE_VERSION_TWO_TIER
    )
    if curve_version == CURVE_VERSION_BOUNDED_FACTOR:
        from ditto.db.queries.confirmation_scores import (
            ConfirmationEfficiencyCosts,
            confirmation_efficiency_costs_by_agent,
        )

        continual_costs = await confirmation_efficiency_costs_by_agent(
            session, agent_ids=agent_ids, bench_version=bench_version
        )
        token_totals = {}
        for agent_id in agent_ids:
            retests = continual_costs.get(agent_id, ConfirmationEfficiencyCosts())
            token_totals[agent_id] = audited_v9_token_total(
                [score.details for score in score_rows.get(agent_id, [])],
                continual_seed_token_totals=retests.seed_token_totals,
                continual_cost_evidence_valid=retests.evidence_valid,
            )
    else:
        token_totals = {
            agent_id: audited_token_total(
                [score.details for score in score_rows.get(agent_id, [])]
            )
            for agent_id in agent_ids
        }
    candidates = _candidates_from_rows(
        rows,
        token_totals,
        curve_version=curve_version,
    )
    epoch = epoch_index_for(now, config.epoch_hours)
    previous = await latest_snapshot(
        session,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        max_epoch_index=epoch - 1,
        active_only=True,
    )
    quality_floor, memory_floor = floors_from_previous(
        previous.members if previous is not None else None,
        quality_floor=config.quality_floor,
        memory_floor=config.memory_floor,
        previous_curve_version=(
            previous.curve_version if previous is not None else None
        ),
        current_curve_version=curve_version,
    )
    reference = build_cohort_snapshot(
        candidates,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        epoch_index=epoch,
        cohort_limit=config.cohort_size,
        n_min=config.min_cohort,
        bonus_cap=config.cap,
        quality_floor=quality_floor,
        memory_floor=memory_floor,
        deep_bonus_cap=(
            config.deep_cap if curve_version == CURVE_VERSION_TWO_TIER else None
        ),
        deep_frontier_ratio=(
            config.deep_frontier_ratio
            if curve_version == CURVE_VERSION_TWO_TIER
            else None
        ),
        curve_version=curve_version,
        factor_alpha=(
            config.factor_alpha
            if curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else None
        ),
        minimum_factor=(
            config.minimum_factor
            if curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else None
        ),
        maximum_factor=(
            config.maximum_factor
            if curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else None
        ),
    )
    cost_evidence = [
        candidate for candidate in candidates if candidate.token_total is not None
    ]
    quality_qualified = [
        candidate
        for candidate in cost_evidence
        if qualifies(
            candidate.composite,
            candidate.memory_mean,
            candidate.token_total,
            quality_floor=quality_floor,
            memory_floor=memory_floor,
        )
    ]
    owner_deduped = dedupe_owners(quality_qualified)
    lineage_deduped = dedupe_lineages(owner_deduped)
    return EfficiencyBoardView(
        snapshot=None,
        bonuses={},
        preview=True,
        preview_reference=reference,
        preview_bonuses=(
            None
            if curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else {
                candidate.agent_id: bonus_for_submission(
                    candidate.composite,
                    candidate.memory_mean,
                    candidate.token_total,
                    reference,
                )
                for candidate in candidates
            }
        ),
        preview_factors=(
            {
                candidate.agent_id: factor
                for candidate in candidates
                if (
                    factor := factor_for_submission(
                        candidate.composite,
                        candidate.memory_mean,
                        candidate.token_total,
                        reference,
                    )
                )
                is not None
            }
            if curve_version == CURVE_VERSION_BOUNDED_FACTOR
            else None
        ),
        preview_candidate_count=len(candidates),
        preview_cost_evidence_count=len(cost_evidence),
        preview_quality_qualified_count=len(quality_qualified),
        preview_owner_deduped_count=len(owner_deduped),
        preview_lineage_deduped_count=len(lineage_deduped),
    )

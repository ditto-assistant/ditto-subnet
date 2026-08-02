"""Platform-side relative token-efficiency bonus for bench_version >= 7.

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
  the cohort's audited chat token totals is the full-bonus frontier and the
  cohort median is the zero-bonus point — never the mean, never the single
  minimum.
* The **bonus** is strictly additive and capped inside the 5-10% envelope:
  tier 1 pays up to the base cap (default 5%) at the P25 frontier; tier 2
  keeps climbing to the deep cap (default 10%) down to the deep frontier
  (``deep_frontier_ratio x P25``) and then SATURATES flat — no reward for
  racing toward zero tokens. ``effective_composite = composite * (1 +
  bonus)``. The validator composite is never touched; the bonus is a
  separate platform-side field.
* **Frozen cohorts**: the snapshot (membership, floors, reference values) is
  computed once per epoch and persisted; a submission's bonus is assigned
  once, against the frozen reference of the epoch it finalized in, and never
  recomputed. Published historical scores never move.
* **Activation gate**: until a cohort has at least ``n_min`` deduped
  qualified members, the snapshot is inactive and every bonus is zero.

The deterministic validator scorer is NEVER touched by any of this: same
artifact, same validator score, forever. bench_version < 7 boards are
byte-identical to their pre-bonus behavior — no snapshot is ever computed
and no bonus row is ever written for them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import ceil
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


@dataclass(frozen=True)
class EfficiencyCandidate:
    """One finalized, ranked submission considered for a cohort."""

    agent_id: UUID
    miner_hotkey: str
    lineage_key: str
    composite: float
    memory_mean: float
    token_total: float | None
    """Median audited chat ``total_tokens`` across the agent's quorum score
    rows whose relay accounting is complete; ``None`` when no quorum row
    carries complete audited usage (such a run can never earn a bonus)."""
    first_seen: datetime


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
                collapsed_agent_ids=tuple(c.agent_id for c in ordered[1:]),
            )
        )
    members.sort(key=lambda m: (-m.composite, str(m.agent_id)))
    return members


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
    members = dedupe_lineages(qualified)[:cohort_limit]
    active = len(members) >= n_min
    p25: float | None = None
    med: float | None = None
    if active:
        totals = [member.token_total for member in members]
        p25 = nearest_rank_percentile(totals, FRONTIER_QUANTILE)
        med = float(median(totals))
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
        curve_version=(
            CURVE_VERSION_TWO_TIER
            if deep_bonus_cap is not None and deep_frontier_ratio is not None
            else CURVE_VERSION_SINGLE_TIER
        ),
        deep_bonus_cap=deep_bonus_cap,
        deep_frontier_ratio=deep_frontier_ratio,
    )


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
    two_tier = reference.curve_version >= CURVE_VERSION_TWO_TIER
    return bonus_fraction(
        token_total,
        reference_p25=reference.reference_p25_tokens,
        reference_median=reference.reference_median_tokens,
        cap=reference.bonus_cap,
        deep_cap=reference.deep_bonus_cap if two_tier else None,
        deep_frontier_ratio=reference.deep_frontier_ratio if two_tier else None,
    )


def effective_composite(composite: float, bonus: float) -> float:
    """The platform-side ranking score: ``composite * (1 + bonus)``.

    Multiplicative, matching the spec's ``bonus_multiplier`` form: the fold
    compares composites, so a scale factor preserves ordering semantics and a
    zero composite can never buy weight from cheapness alone. Bounded by
    ``1 + cap`` (<= 1.10). The validator's composite is never modified."""
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
) -> tuple[float, float]:
    """Derive this epoch's quality floors from the previous ACTIVE cohort.

    Spec policy: ``Q_min`` = previous cohort's median composite, ``M_min`` =
    ``MEMORY_FLOOR_FRACTION x`` previous median memory_mean — both never
    below the configured static floors, which alone govern the first epoch.
    """
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


def _candidates_from_rows(
    rows: Sequence[LedgerRow],
    token_totals: Mapping[UUID, float | None],
) -> list[EfficiencyCandidate]:
    return [
        EfficiencyCandidate(
            agent_id=row.agent_id,
            miner_hotkey=row.miner_hotkey,
            lineage_key=lineage_key(row.normalized_source_hash, row.sha256),
            composite=row.composite,
            memory_mean=row.memory_mean,
            token_total=token_totals.get(row.agent_id),
            first_seen=row.first_seen,
        )
        for row in rows
    ]


async def _finalized_ranked_rows(session: AsyncSession) -> list[LedgerRow]:
    """The authoritative board's finalized (quorum), ranked rows."""
    from ditto.db.queries.scores import (
        SCORING_QUORUM,
        get_score_counts,
        list_eligible_ledger,
    )

    rows = await list_eligible_ledger(
        session, include_fingerprints=False, include_details=False
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
    score_rows = await quorum_score_rows(session, agent_ids, bench_versions=versions)
    token_totals = {
        agent_id: audited_token_total(
            [score.details for score in score_rows.get(agent_id, [])]
        )
        for agent_id in agent_ids
    }
    candidates = _candidates_from_rows(rows, token_totals)

    snapshot = await get_snapshot(
        session,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        epoch_index=epoch,
    )
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
            deep_bonus_cap=config.deep_cap,
            deep_frontier_ratio=config.deep_frontier_ratio,
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
    existing = await get_bonus_rows(
        session, agent_ids, bench_versions=versions, epoch_index=epoch
    )
    for candidate in candidates:
        if candidate.agent_id in existing:
            continue
        bonus = bonus_for_submission(
            candidate.composite,
            candidate.memory_mean,
            candidate.token_total,
            reference,
        )
        await insert_bonus(
            session,
            agent_id=candidate.agent_id,
            bench_version=bench_version,
            epoch_index=epoch,
            snapshot_id=snapshot.snapshot_id,
            token_total=candidate.token_total,
            bonus=bonus,
        )


def reference_from_snapshot(snapshot: EfficiencyCohortSnapshot) -> CohortReference:
    """Rehydrate the pure reference from a persisted snapshot row, so a bonus
    is always reproducible from stored data alone."""
    members: list[CohortMember] = []
    for raw in snapshot.members or []:
        if not isinstance(raw, Mapping):
            continue
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
    )


async def read_efficiency_board(
    session: AsyncSession,
    config: EfficiencyBonusConfig,
    *,
    bench_version: int,
    agent_ids: Sequence[UUID],
    bench_versions: Mapping[UUID, int],
    now: datetime,
) -> EfficiencyBoardView | None:
    """Read-only view for a board render: the governing snapshot (the current
    epoch's, else the latest frozen one) plus the displayed agents' frozen
    bonus rows. ``None`` when the bonus does not apply to this board at all
    (disabled, or bench_version < 7)."""
    from ditto.db.queries.efficiency import get_bonus_rows, latest_snapshot

    if not config.enabled or bench_version < MIN_BONUS_BENCH_VERSION:
        return None
    snapshot = await latest_snapshot(
        session,
        bench_version=bench_version,
        run_size=BONUS_RUN_SIZE,
        max_epoch_index=epoch_index_for(now, config.epoch_hours),
        active_only=False,
    )
    bonuses = await get_bonus_rows(
        session,
        agent_ids,
        bench_versions=bench_versions,
        epoch_index=epoch_index_for(now, config.epoch_hours),
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
    candidates = _candidates_from_rows(
        rows,
        {
            agent_id: audited_token_total(
                [score.details for score in score_rows.get(agent_id, [])]
            )
            for agent_id in agent_ids
        },
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
        deep_bonus_cap=config.deep_cap,
        deep_frontier_ratio=config.deep_frontier_ratio,
    )
    return EfficiencyBoardView(
        snapshot=None,
        bonuses={},
        preview=True,
        preview_reference=reference,
        preview_bonuses={
            candidate.agent_id: bonus_for_submission(
                candidate.composite,
                candidate.memory_mean,
                candidate.token_total,
                reference,
            )
            for candidate in candidates
        },
    )

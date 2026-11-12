"""How fast the competition improves: frontier, drivers, forks and noise.

Measurement only. Nothing here changes a score, a status, a hold, the
release policy or a weight. The job (:mod:`competition_improvement_job`)
loads durable facts, calls these pure functions and stores the result; the
operator read aggregates the stored rows. Keeping the arithmetic pure makes
every number reproducible from the inputs a test can write down.

Definitions (also returned on the wire so a reader never has to guess):

* **canonical composite** -- the median of an agent's k=3 quorum composites
  on one bench version (the score a historical snapshot publishes; the
  continual mean is not reconstructed).
* **frontier(v, d)** -- the highest canonical composite among ``scored`` /
  ``live`` agents on bench ``v`` whose quorum finalized before the end of UTC
  day ``d``.
* **top-N** -- the frontier set deduplicated to one agent per owner (hotkey
  and payment coldkey are one owner).
* **new high** -- an agent whose composite strictly exceeds the frontier at
  the moment its quorum finalized. Its *driver* is ``released_fork`` when it
  is a detected fork of a source release, ``same_owner_iteration`` when its
  owner uploaded anything before it, else ``original``.
* **source release** -- the first exact-SHA public artifact handoff.
* **fork** -- a later upload by another owner that contains at least
  :data:`FORK_CONTAINMENT` of the release's distinctive items, or that
  declared the release as its screening ``released_base``. Its *novel
  fraction* is the share of its own distinctive items the base lacks; a fork
  under :data:`THIN_FORK_NOVEL_FRACTION` is *thin*.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

METRICS_VERSION = "cim1"
FIRST_BENCH_VERSION = 13
FORK_CONTAINMENT = 0.5
THIN_FORK_NOVEL_FRACTION = 0.10
TOP_N = 5
NOISE_AGENTS = 10
MIN_NOISE_SEEDS = 3
# A new high is called "beyond noise" when its step clears this many standard
# deviations of a single-seed difference between two agents.
NOISE_SIGMAS = 2.0

Driver = Literal["original", "same_owner_iteration", "released_fork"]

DEFINITIONS: dict[str, str] = {
    "canonical_composite": (
        "median of the agent's k=3 quorum composites on one bench version"
    ),
    "frontier": (
        "highest canonical composite among scored/live agents finalized by "
        "the end of the UTC day"
    ),
    "top_n": (
        "frontier set deduplicated to one agent per owner (hotkey + payment coldkey)"
    ),
    "new_high": (
        "agent whose composite strictly exceeds the frontier when its quorum finalized"
    ),
    "driver": (
        "released_fork if a detected fork of a release, same_owner_iteration "
        "if its owner uploaded earlier, else original"
    ),
    "source_release": "first exact-SHA public artifact handoff",
    "fork": (
        f"other-owner upload after the release holding >= {FORK_CONTAINMENT:g} "
        "of the release's distinctive items, or declaring it as released_base"
    ),
    "thin_fork": (
        "fork whose novel distinctive-item fraction is below "
        f"{THIN_FORK_NOVEL_FRACTION:g}"
    ),
    "seed_sd": (
        "standard deviation of an agent's per-seed composites (validator "
        "median per seed)"
    ),
    "single_seed_delta_sd": (
        "sqrt(2) x median seed_sd: the spread of a one-seed difference "
        "between two agents"
    ),
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def day_end(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0), tzinfo=UTC)


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 3)


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


@dataclass(frozen=True)
class ScoredAgent:
    agent_id: UUID
    owner: str
    name: str
    version: int | None
    bench_version: int
    composite: float
    finalized_at: datetime
    uploaded_at: datetime
    status: str


@dataclass(frozen=True)
class SourceRelease:
    agent_id: UUID
    owner: str
    name: str
    version: int | None
    bench_version: int | None
    released_at: datetime
    uploaded_at: datetime
    embargo_hours: int
    disclosure: str
    base_composite: float | None


@dataclass(frozen=True)
class ForkCandidate:
    """One later other-owner upload measured against one release."""

    base_agent_id: UUID
    fork_agent_id: UUID
    fork_owner: str
    fork_name: str
    fork_version: int | None
    fork_status: str
    fork_uploaded_at: datetime
    containment: float | None
    novel_fraction: float | None
    declared: bool


@dataclass(frozen=True)
class ForkRow:
    base_agent_id: UUID
    fork_agent_id: UUID
    fork_name: str
    fork_version: int | None
    fork_status: str
    bench_version: int | None
    released_at: datetime
    fork_uploaded_at: datetime
    lag_hours: float
    containment: float | None
    novel_fraction: float | None
    declared: bool
    thin: bool
    base_composite: float | None
    fork_composite: float | None
    delta: float | None
    shared_seed_count: int
    shared_seed_delta: float | None


@dataclass(frozen=True)
class PolicyRevision:
    revision: int
    embargo_hours: int
    disclosure: str
    created_at: datetime | None


@dataclass(frozen=True)
class ChampionPin:
    bench_version: int
    pinned_at: datetime
    agent_id: UUID | None


@dataclass(frozen=True)
class NewHigh:
    agent_id: UUID
    name: str
    version: int | None
    bench_version: int
    finalized_at: datetime
    composite: float
    previous_frontier: float
    delta: float
    days_since_previous_high: float | None
    driver: Driver
    base_agent_id: UUID | None


@dataclass(frozen=True)
class TopEntry:
    agent_id: UUID
    name: str
    version: int | None
    composite: float


@dataclass(frozen=True)
class DailyRow:
    bench_version: int
    day: date
    frontier_composite: float | None
    frontier_agent_id: UUID | None
    champion_agent_id: UUID | None
    champion_composite: float | None
    top_n_mean: float | None
    top_n_min: float | None
    finalized_agent_count: int
    new_high_count: int
    releases_on_day: int
    releases_to_date: int
    forks_on_day: int
    forks_to_date: int
    thin_forks_to_date: int
    embargo_hours: int
    disclosure: str
    top: tuple[TopEntry, ...] = ()
    new_highs: tuple[NewHigh, ...] = ()
    release_agent_ids: tuple[UUID, ...] = ()
    fork_agent_ids: tuple[UUID, ...] = ()


def policy_at(
    revisions: Sequence[PolicyRevision],
    at: datetime,
    *,
    default: PolicyRevision,
) -> PolicyRevision:
    """The revision in force at ``at`` (append-only history)."""
    current = default
    for revision in sorted(revisions, key=lambda r: r.revision):
        if revision.created_at is not None and _utc(revision.created_at) <= at:
            current = revision
    return current


def classify_forks(
    releases: Sequence[SourceRelease],
    candidates: Iterable[ForkCandidate],
    *,
    composites: Mapping[tuple[UUID, int], float],
    seed_scores: Mapping[tuple[UUID, int], Mapping[int, float]],
) -> list[ForkRow]:
    """Detected forks of each release with lag, size of change and delta.

    ``composites`` and ``seed_scores`` are keyed ``(agent_id, bench_version)``.
    The delta compares the fork with its base on the base's bench version;
    the shared-seed delta pairs the two on every seed both were scored on.
    Each fork is attributed to exactly one base.
    """
    by_id = {release.agent_id: release for release in releases}
    rows: list[ForkRow] = []
    for candidate in candidates:
        release = by_id.get(candidate.base_agent_id)
        if release is None or candidate.fork_uploaded_at <= release.released_at:
            continue
        if candidate.fork_owner == release.owner:
            continue
        contained = (
            candidate.containment is not None
            and candidate.containment >= FORK_CONTAINMENT
        )
        if not (contained or candidate.declared):
            continue
        bench = release.bench_version
        fork_composite = (
            composites.get((candidate.fork_agent_id, bench))
            if bench is not None
            else None
        )
        base_composite = (
            composites.get((release.agent_id, bench)) if bench is not None else None
        )
        shared: list[float] = []
        if bench is not None:
            base_seeds = seed_scores.get((release.agent_id, bench), {})
            fork_seeds = seed_scores.get((candidate.fork_agent_id, bench), {})
            shared = [
                fork_seeds[seed] - base_seeds[seed]
                for seed in sorted(set(base_seeds) & set(fork_seeds))
            ]
        rows.append(
            ForkRow(
                base_agent_id=release.agent_id,
                fork_agent_id=candidate.fork_agent_id,
                fork_name=candidate.fork_name,
                fork_version=candidate.fork_version,
                fork_status=candidate.fork_status,
                bench_version=bench,
                released_at=release.released_at,
                fork_uploaded_at=candidate.fork_uploaded_at,
                lag_hours=_hours(candidate.fork_uploaded_at - release.released_at),
                containment=candidate.containment,
                novel_fraction=candidate.novel_fraction,
                declared=candidate.declared,
                thin=(
                    candidate.novel_fraction is not None
                    and candidate.novel_fraction < THIN_FORK_NOVEL_FRACTION
                ),
                base_composite=base_composite,
                fork_composite=fork_composite,
                delta=(
                    round(fork_composite - base_composite, 6)
                    if fork_composite is not None and base_composite is not None
                    else None
                ),
                shared_seed_count=len(shared),
                shared_seed_delta=(
                    round(statistics.fmean(shared), 6) if shared else None
                ),
            )
        )
    # One base per fork. Successive releases of one line share most of their
    # items, so a fork of the newest one also contains the older ones; counting
    # it under each would inflate forks per release and shorten nothing. A
    # declared base wins, then the highest containment, then the latest
    # release among near-equal containments.
    chosen: dict[UUID, ForkRow] = {}
    for row in rows:
        held = chosen.get(row.fork_agent_id)
        if held is None or _base_rank(row) > _base_rank(held):
            chosen[row.fork_agent_id] = row
    out = sorted(
        chosen.values(),
        key=lambda r: (r.released_at, r.fork_uploaded_at, str(r.fork_agent_id)),
    )
    return out


def _base_rank(row: ForkRow) -> tuple[bool, float, datetime]:
    containment = round(row.containment or 0.0, 2)
    return (row.declared, containment, row.released_at)


def find_new_highs(
    agents: Sequence[ScoredAgent],
    *,
    forks: Sequence[ForkRow],
    owner_first_upload: Mapping[str, datetime],
) -> list[NewHigh]:
    """Every strict improvement of one bench version's frontier, in order.

    The first finalized agent sets the baseline and is not a new high.
    ``owner_first_upload`` is each owner's earliest upload of any agent on any
    bench, so a first-ever upload that sets a record is ``original``.
    """
    base_of: dict[UUID, UUID] = {}
    for fork in sorted(forks, key=lambda f: f.containment or 0.0, reverse=True):
        base_of.setdefault(fork.fork_agent_id, fork.base_agent_id)
    ordered = sorted(agents, key=lambda a: (a.finalized_at, str(a.agent_id)))
    highs: list[NewHigh] = []
    frontier: float | None = None
    last_high_at: datetime | None = None
    for agent in ordered:
        if frontier is None:
            frontier, last_high_at = agent.composite, agent.finalized_at
            continue
        if agent.composite <= frontier:
            continue
        base = base_of.get(agent.agent_id)
        first = owner_first_upload.get(agent.owner)
        driver: Driver = (
            "released_fork"
            if base is not None
            else (
                "same_owner_iteration"
                if first is not None and first < agent.uploaded_at
                else "original"
            )
        )
        highs.append(
            NewHigh(
                agent_id=agent.agent_id,
                name=agent.name,
                version=agent.version,
                bench_version=agent.bench_version,
                finalized_at=agent.finalized_at,
                composite=agent.composite,
                previous_frontier=frontier,
                delta=round(agent.composite - frontier, 6),
                days_since_previous_high=(
                    round(
                        (agent.finalized_at - last_high_at).total_seconds() / 86400, 3
                    )
                    if last_high_at is not None
                    else None
                ),
                driver=driver,
                base_agent_id=base,
            )
        )
        frontier, last_high_at = agent.composite, agent.finalized_at
    return highs


def _top(agents: Iterable[ScoredAgent], n: int) -> list[ScoredAgent]:
    best: dict[str, ScoredAgent] = {}
    for agent in agents:
        held = best.get(agent.owner)
        if held is None or (agent.composite, -agent.finalized_at.timestamp()) > (
            held.composite,
            -held.finalized_at.timestamp(),
        ):
            best[agent.owner] = agent
    return sorted(
        best.values(), key=lambda a: (-a.composite, a.finalized_at, str(a.agent_id))
    )[:n]


def build_daily(
    bench_version: int,
    agents: Sequence[ScoredAgent],
    *,
    first_day: date,
    last_day: date,
    champions: Sequence[ChampionPin],
    releases: Sequence[SourceRelease],
    forks: Sequence[ForkRow],
    new_highs: Sequence[NewHigh],
    revisions: Sequence[PolicyRevision],
    default_policy: PolicyRevision,
    top_n: int = TOP_N,
) -> list[DailyRow]:
    """One row per UTC day of one bench version's era."""
    composites = {a.agent_id: a.composite for a in agents}
    pins = sorted(
        (p for p in champions if p.bench_version == bench_version),
        key=lambda p: p.pinned_at,
    )
    on_bench = [r for r in releases if r.bench_version == bench_version]
    bench_forks = [f for f in forks if f.bench_version == bench_version]
    rows: list[DailyRow] = []
    day = first_day
    while day <= last_day:
        end = day_end(day)
        start = end - timedelta(days=1)
        finalized = [a for a in agents if a.finalized_at < end]
        top = _top(finalized, top_n)
        frontier = top[0] if top else None
        champion_id: UUID | None = None
        for pin in pins:
            if pin.pinned_at < end:
                champion_id = pin.agent_id
        highs = tuple(h for h in new_highs if start <= h.finalized_at < end)
        released_today = tuple(
            r.agent_id for r in on_bench if start <= r.released_at < end
        )
        forked_today = tuple(
            f.fork_agent_id for f in bench_forks if start <= f.fork_uploaded_at < end
        )
        forks_to_date = [f for f in bench_forks if f.fork_uploaded_at < end]
        policy = policy_at(revisions, end, default=default_policy)
        rows.append(
            DailyRow(
                bench_version=bench_version,
                day=day,
                frontier_composite=frontier.composite if frontier else None,
                frontier_agent_id=frontier.agent_id if frontier else None,
                champion_agent_id=champion_id,
                champion_composite=composites.get(champion_id) if champion_id else None,
                top_n_mean=(
                    round(statistics.fmean(a.composite for a in top), 6)
                    if top
                    else None
                ),
                top_n_min=top[-1].composite if top else None,
                finalized_agent_count=len(finalized),
                new_high_count=len(highs),
                releases_on_day=len(released_today),
                releases_to_date=sum(1 for r in on_bench if r.released_at < end),
                forks_on_day=len(forked_today),
                forks_to_date=len(forks_to_date),
                thin_forks_to_date=sum(1 for f in forks_to_date if f.thin),
                embargo_hours=policy.embargo_hours,
                disclosure=policy.disclosure,
                top=tuple(
                    TopEntry(a.agent_id, a.name, a.version, a.composite) for a in top
                ),
                new_highs=highs,
                release_agent_ids=released_today,
                fork_agent_ids=forked_today,
            )
        )
        day += timedelta(days=1)
    return rows


# ---------------------------------------------------------------- read rollups


@dataclass(frozen=True)
class NoiseAgent:
    agent_id: UUID
    name: str
    version: int | None
    composite: float
    seed_count: int
    seed_sd: float | None
    quorum_sem: float | None


@dataclass(frozen=True)
class NoiseFloor:
    bench_version: int
    agents_measured: int
    median_seed_sd: float | None
    max_seed_sd: float | None
    median_quorum_sem: float | None
    single_seed_delta_sd: float | None
    beyond_noise_step: float | None
    agents: tuple[NoiseAgent, ...]


def noise_floor(
    bench_version: int,
    agents: Sequence[ScoredAgent],
    *,
    seed_scores: Mapping[tuple[UUID, int], Mapping[int, float]],
    quorum: Mapping[tuple[UUID, int], Sequence[float]],
    top: int = NOISE_AGENTS,
) -> NoiseFloor:
    """Seed-to-seed and validator-to-validator spread of the top agents."""
    leaders = sorted(agents, key=lambda a: (-a.composite, a.finalized_at))[:top]
    measured: list[NoiseAgent] = []
    for agent in leaders:
        seeds = list(seed_scores.get((agent.agent_id, bench_version), {}).values())
        votes = list(quorum.get((agent.agent_id, bench_version), ()))
        measured.append(
            NoiseAgent(
                agent_id=agent.agent_id,
                name=agent.name,
                version=agent.version,
                composite=agent.composite,
                seed_count=len(seeds),
                seed_sd=(
                    round(statistics.stdev(seeds), 6)
                    if len(seeds) >= MIN_NOISE_SEEDS
                    else None
                ),
                quorum_sem=(
                    round(statistics.stdev(votes) / math.sqrt(len(votes)), 6)
                    if len(votes) >= 2
                    else None
                ),
            )
        )
    sds = [a.seed_sd for a in measured if a.seed_sd is not None]
    sems = [a.quorum_sem for a in measured if a.quorum_sem is not None]
    median_sd = _median(sds)
    delta_sd = round(median_sd * math.sqrt(2), 6) if median_sd is not None else None
    return NoiseFloor(
        bench_version=bench_version,
        agents_measured=len(sds),
        median_seed_sd=round(median_sd, 6) if median_sd is not None else None,
        max_seed_sd=max(sds) if sds else None,
        median_quorum_sem=round(m, 6) if (m := _median(sems)) is not None else None,
        single_seed_delta_sd=delta_sd,
        beyond_noise_step=(
            round(delta_sd * NOISE_SIGMAS, 6) if delta_sd is not None else None
        ),
        agents=tuple(measured),
    )


@dataclass(frozen=True)
class BenchSummary:
    bench_version: int
    first_day: date | None
    last_day: date | None
    days: int
    frontier_start: float | None
    frontier_end: float | None
    frontier_gain: float | None
    gain_per_day: float | None
    new_high_count: int
    beyond_noise_high_count: int | None
    median_days_between_highs: float | None
    max_days_between_highs: float | None
    days_since_last_high: float | None
    champion_changes: int
    drivers: dict[str, int]
    gain_by_driver: dict[str, float]
    largest_steps: tuple[NewHigh, ...] = field(default=())


def summarize_bench(
    bench_version: int,
    daily: Sequence[DailyRow],
    highs: Sequence[NewHigh],
    *,
    now: datetime,
    beyond_noise_step: float | None,
) -> BenchSummary:
    rows = sorted(
        (r for r in daily if r.bench_version == bench_version), key=lambda r: r.day
    )
    valued = [r for r in rows if r.frontier_composite is not None]
    start = valued[0].frontier_composite if valued else None
    end = valued[-1].frontier_composite if valued else None
    span = (valued[-1].day - valued[0].day).days if len(valued) > 1 else 0
    gaps = [
        h.days_since_previous_high
        for h in highs
        if h.days_since_previous_high is not None
    ]
    drivers = {"original": 0, "same_owner_iteration": 0, "released_fork": 0}
    gain: dict[str, float] = dict.fromkeys(drivers, 0.0)
    for high in highs:
        drivers[high.driver] += 1
        gain[high.driver] = round(gain[high.driver] + high.delta, 6)
    changes = 0
    previous: UUID | None = None
    for row in rows:
        if row.champion_agent_id is not None and row.champion_agent_id != previous:
            changes += previous is not None
            previous = row.champion_agent_id
    last_high = max((h.finalized_at for h in highs), default=None)
    return BenchSummary(
        bench_version=bench_version,
        first_day=rows[0].day if rows else None,
        last_day=rows[-1].day if rows else None,
        days=len(rows),
        frontier_start=start,
        frontier_end=end,
        frontier_gain=round(end - start, 6)
        if start is not None and end is not None
        else None,
        gain_per_day=(
            round((end - start) / span, 6)
            if start is not None and end is not None and span > 0
            else None
        ),
        new_high_count=len(highs),
        beyond_noise_high_count=(
            sum(1 for h in highs if h.delta >= beyond_noise_step)
            if beyond_noise_step is not None
            else None
        ),
        median_days_between_highs=round(m, 3)
        if (m := _median(gaps)) is not None
        else None,
        max_days_between_highs=max(gaps) if gaps else None,
        days_since_last_high=(
            round((now - last_high).total_seconds() / 86400, 3) if last_high else None
        ),
        champion_changes=changes,
        drivers=drivers,
        gain_by_driver=gain,
        largest_steps=tuple(sorted(highs, key=lambda h: -h.delta)[:3]),
    )


@dataclass(frozen=True)
class ReleaseSummary:
    release: SourceRelease
    fork_count: int
    thin_fork_count: int
    first_fork_lag_hours: float | None
    median_fork_lag_hours: float | None
    median_novel_fraction: float | None
    median_delta: float | None
    best_delta: float | None
    forks_beating_base: int
    forks: tuple[ForkRow, ...]


def summarize_releases(
    releases: Sequence[SourceRelease], forks: Sequence[ForkRow]
) -> list[ReleaseSummary]:
    out: list[ReleaseSummary] = []
    for release in sorted(releases, key=lambda r: r.released_at):
        own = [f for f in forks if f.base_agent_id == release.agent_id]
        lags = sorted(f.lag_hours for f in own)
        deltas = [f.delta for f in own if f.delta is not None]
        novel = [f.novel_fraction for f in own if f.novel_fraction is not None]
        out.append(
            ReleaseSummary(
                release=release,
                fork_count=len(own),
                thin_fork_count=sum(1 for f in own if f.thin),
                first_fork_lag_hours=lags[0] if lags else None,
                median_fork_lag_hours=_median(lags),
                median_novel_fraction=_median(novel),
                median_delta=_median(deltas),
                best_delta=max(deltas) if deltas else None,
                forks_beating_base=sum(1 for d in deltas if d > 0),
                forks=tuple(own),
            )
        )
    return out


@dataclass(frozen=True)
class EmbargoWindow:
    revision: int
    embargo_hours: int
    disclosure: str
    starts_at: datetime | None
    ends_at: datetime | None
    days: float | None
    releases: int
    forks: int
    forks_per_release: float | None
    thin_forks: int
    thin_fork_share: float | None
    median_fork_lag_hours: float | None
    median_fork_delta: float | None
    new_highs: int
    new_highs_by_driver: dict[str, int]
    frontier_gain_by_bench: dict[int, float]
    gain_per_day_by_bench: dict[int, float]


def embargo_windows(
    revisions: Sequence[PolicyRevision],
    *,
    default_policy: PolicyRevision,
    releases: Sequence[SourceRelease],
    forks: Sequence[ForkRow],
    highs: Sequence[NewHigh],
    daily: Sequence[DailyRow],
    now: datetime,
) -> list[EmbargoWindow]:
    """Releases, forks and frontier movement grouped by the policy in force.

    A release belongs to the window its handoff happened in (the policy that
    timed it); its forks follow the release wherever they land. A new high
    belongs to the window its quorum finalized in.
    """
    # Spans follow wall-clock order; revisions are written by one CAS chain, so
    # this is also revision order in production.
    ordered = sorted(
        (r for r in revisions if r.created_at is not None),
        key=lambda r: (_utc(r.created_at), r.revision),  # type: ignore[arg-type]
    )
    spans: list[tuple[PolicyRevision, datetime | None, datetime | None]] = []
    starts: list[datetime | None] = [None, *(_utc(r.created_at) for r in ordered)]  # type: ignore[arg-type]
    policies = [default_policy, *ordered]
    for index, policy in enumerate(policies):
        ends = starts[index + 1] if index + 1 < len(starts) else None
        spans.append((policy, starts[index], ends))
    # The pre-history default window is only kept when something happened in it.
    out: list[EmbargoWindow] = []
    for policy, start, end in spans:

        def inside(
            at: datetime, start: datetime | None = start, end: datetime | None = end
        ) -> bool:
            return (start is None or at >= start) and (end is None or at < end)

        window_releases = [r for r in releases if inside(r.released_at)]
        ids = {r.agent_id for r in window_releases}
        window_forks = [f for f in forks if f.base_agent_id in ids]
        window_highs = [h for h in highs if inside(h.finalized_at)]
        if start is None and not window_releases and not window_highs:
            continue
        by_driver = {"original": 0, "same_owner_iteration": 0, "released_fork": 0}
        for high in window_highs:
            by_driver[high.driver] += 1
        gains: dict[int, float] = {}
        per_day: dict[int, float] = {}
        for bench in sorted({r.bench_version for r in daily}):
            rows = sorted(
                (
                    r
                    for r in daily
                    if r.bench_version == bench
                    and r.frontier_composite is not None
                    and inside(day_end(r.day) - timedelta(seconds=1))
                ),
                key=lambda r: r.day,
            )
            if len(rows) >= 2:
                gain = rows[-1].frontier_composite - rows[0].frontier_composite  # type: ignore[operator]
                gains[bench] = round(gain, 6)
                per_day[bench] = round(gain / (rows[-1].day - rows[0].day).days, 6)
        lags = [f.lag_hours for f in window_forks]
        deltas = [f.delta for f in window_forks if f.delta is not None]
        thin = sum(1 for f in window_forks if f.thin)
        effective_end = end or now
        out.append(
            EmbargoWindow(
                revision=policy.revision,
                embargo_hours=policy.embargo_hours,
                disclosure=policy.disclosure,
                starts_at=start,
                ends_at=end,
                days=(
                    round((effective_end - start).total_seconds() / 86400, 3)
                    if start is not None
                    else None
                ),
                releases=len(window_releases),
                forks=len(window_forks),
                forks_per_release=(
                    round(len(window_forks) / len(window_releases), 3)
                    if window_releases
                    else None
                ),
                thin_forks=thin,
                thin_fork_share=(
                    round(thin / len(window_forks), 3) if window_forks else None
                ),
                median_fork_lag_hours=_median(lags),
                median_fork_delta=_median(deltas),
                new_highs=len(window_highs),
                new_highs_by_driver=by_driver,
                frontier_gain_by_bench=gains,
                gain_per_day_by_bench=per_day,
            )
        )
    return out


def filter_since(rows: Sequence[DailyRow], since: date | None) -> list[DailyRow]:
    return [r for r in rows if since is None or r.day >= since]


__all__ = [
    "DEFINITIONS",
    "FIRST_BENCH_VERSION",
    "FORK_CONTAINMENT",
    "METRICS_VERSION",
    "THIN_FORK_NOVEL_FRACTION",
    "TOP_N",
    "BenchSummary",
    "ChampionPin",
    "DailyRow",
    "EmbargoWindow",
    "ForkCandidate",
    "ForkRow",
    "NewHigh",
    "NoiseFloor",
    "PolicyRevision",
    "ReleaseSummary",
    "ScoredAgent",
    "SourceRelease",
    "TopEntry",
    "build_daily",
    "classify_forks",
    "day_end",
    "embargo_windows",
    "filter_since",
    "find_new_highs",
    "noise_floor",
    "policy_at",
    "summarize_bench",
    "summarize_releases",
]

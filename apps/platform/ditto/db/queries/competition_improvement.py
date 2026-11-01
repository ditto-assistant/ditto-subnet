"""Inputs and storage for the competition improvement metrics.

Read-only over every source table. The only writes are to the three
measurement tables this feature owns; nothing here touches a score, a status,
a hold, the release policy or a weight.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.competition_improvement import (
    FIRST_BENCH_VERSION,
    METRICS_VERSION,
    ChampionPin,
    DailyRow,
    ForkCandidate,
    ForkRow,
    NewHigh,
    PolicyRevision,
    ScoredAgent,
    SourceRelease,
    TopEntry,
    build_daily,
    classify_forks,
    find_new_highs,
    policy_at,
)
from ditto.api_server.distinctive_content import ITEMS_VERSION
from ditto.api_server.unreleased_copy import canonical_owners
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    ArtifactFetchAudit,
    ArtifactReleaseSettingsRevision,
    CompetitionImprovementDaily,
    CompetitionReleaseFork,
    CompetitionSourceRelease,
    ConfirmationScore,
    EvaluationPayment,
    LedgerEpochSnapshot,
    Score,
    ScreeningAttempt,
)
from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
    DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
)
from ditto.db.queries.confirmation_scores import not_voided
from ditto.db.queries.review_lineage import REFERENCE_RELEASED_BASE
from ditto.db.queries.scores import SCORING_QUORUM

FRONTIER_STATUSES = frozenset({AgentStatus.SCORED.value, AgentStatus.LIVE.value})
# A day is frozen once it is this many days old: late quorums for it have
# landed, and a later re-score must not rewrite what the frontier was.
CLOSE_AFTER_DAYS = 2

DEFAULT_POLICY = PolicyRevision(
    revision=0,
    embargo_hours=DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
    disclosure=DEFAULT_ARTIFACT_RELEASE_DISCLOSURE.value,
    created_at=None,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _status(value: Any) -> str:
    return value.value if isinstance(value, AgentStatus) else str(value)


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: UUID
    owner: str
    name: str
    version: int | None
    uploaded_at: datetime
    status: str


@dataclass(frozen=True)
class ScoreFacts:
    """Canonical composites, quorum votes and per-seed scores, bench >= floor."""

    agents: list[ScoredAgent]
    composites: dict[tuple[UUID, int], float]
    quorum: dict[tuple[UUID, int], list[float]]
    seed_scores: dict[tuple[UUID, int], dict[int, float]]


async def load_identities(session: AsyncSession) -> dict[UUID, AgentIdentity]:
    """Every agent with its canonical owner (hotkey + payment coldkey)."""
    rows = (
        await session.execute(
            select(
                Agent.agent_id,
                Agent.miner_hotkey,
                Agent.name,
                Agent.version,
                Agent.created_at,
                Agent.status,
            )
        )
    ).all()
    coldkeys = dict(
        (
            await session.execute(
                select(EvaluationPayment.agent_id, EvaluationPayment.miner_coldkey)
            )
        )
        .tuples()
        .all()
    )
    owners = canonical_owners(
        (row.agent_id, row.miner_hotkey, coldkeys.get(row.agent_id)) for row in rows
    )
    return {
        row.agent_id: AgentIdentity(
            agent_id=row.agent_id,
            owner=owners[row.agent_id],
            name=row.name,
            version=row.version,
            uploaded_at=_utc(row.created_at),
            status=_status(row.status),
        )
        for row in rows
    }


def fold_scores(
    rows: Iterable[tuple[UUID, int, float, datetime, int, str]],
    confirmation: Iterable[tuple[UUID, int, int, float]],
    identities: Mapping[UUID, AgentIdentity],
    *,
    statuses: frozenset[str] = FRONTIER_STATUSES,
) -> ScoreFacts:
    """Fold raw score rows into canonical composites and per-seed scores.

    ``rows`` are ``(agent_id, bench_version, composite, created_at, seed,
    validator)``; ``confirmation`` rows are ``(agent_id, bench_version, seed,
    composite)``. A quorum finalizes at its third row's ``created_at`` (the
    platform-controlled first-insert time). A seed's score is the median
    across validators, from the quorum and the confirmation ledger together.
    """
    grouped: dict[tuple[UUID, int], list[tuple[datetime, str, float, int]]] = {}
    for agent_id, bench, composite, created_at, seed, validator in rows:
        grouped.setdefault((agent_id, int(bench)), []).append(
            (_utc(created_at), validator, float(composite), int(seed))
        )
    by_seed: dict[tuple[UUID, int], dict[int, list[float]]] = {}
    for key, votes in grouped.items():
        for _at, _validator, composite, seed in votes:
            by_seed.setdefault(key, {}).setdefault(seed, []).append(composite)
    for agent_id, bench, seed, composite in confirmation:
        by_seed.setdefault((agent_id, int(bench)), {}).setdefault(int(seed), []).append(
            float(composite)
        )
    agents: list[ScoredAgent] = []
    composites: dict[tuple[UUID, int], float] = {}
    quorum: dict[tuple[UUID, int], list[float]] = {}
    for (agent_id, bench), votes in grouped.items():
        if len(votes) < SCORING_QUORUM:
            continue
        votes.sort(key=lambda v: (v[0], v[1]))
        values = [v[2] for v in votes]
        composite = float(statistics.median(values))
        composites[(agent_id, bench)] = composite
        quorum[(agent_id, bench)] = values
        identity = identities.get(agent_id)
        if identity is None or identity.status not in statuses:
            continue
        agents.append(
            ScoredAgent(
                agent_id=agent_id,
                owner=identity.owner,
                name=identity.name,
                version=identity.version,
                bench_version=bench,
                composite=composite,
                finalized_at=votes[SCORING_QUORUM - 1][0],
                uploaded_at=identity.uploaded_at,
                status=identity.status,
            )
        )
    seed_scores = {
        key: {seed: float(statistics.median(v)) for seed, v in seeds.items()}
        for key, seeds in by_seed.items()
    }
    return ScoreFacts(agents, composites, quorum, seed_scores)


async def load_score_facts(
    session: AsyncSession,
    identities: Mapping[UUID, AgentIdentity],
    *,
    bench_versions: Sequence[int] | None = None,
    agent_ids: Sequence[UUID] | None = None,
) -> ScoreFacts:
    score_where = [Score.bench_version >= FIRST_BENCH_VERSION]
    confirmation_where = [
        ConfirmationScore.bench_version >= FIRST_BENCH_VERSION,
        not_voided(),
    ]
    if bench_versions is not None:
        score_where.append(Score.bench_version.in_(bench_versions))
        confirmation_where.append(ConfirmationScore.bench_version.in_(bench_versions))
    if agent_ids is not None:
        score_where.append(Score.agent_id.in_(agent_ids))
        confirmation_where.append(ConfirmationScore.agent_id.in_(agent_ids))
    scores = (
        await session.execute(
            select(
                Score.agent_id,
                Score.bench_version,
                Score.composite,
                Score.created_at,
                Score.seed,
                Score.validator_hotkey,
            ).where(*score_where)
        )
    ).tuples()
    confirmation = (
        await session.execute(
            select(
                ConfirmationScore.agent_id,
                ConfirmationScore.bench_version,
                ConfirmationScore.seed,
                ConfirmationScore.composite,
            ).where(*confirmation_where)
        )
    ).tuples()
    return fold_scores(scores.all(), confirmation.all(), identities)


async def load_revisions(session: AsyncSession) -> list[PolicyRevision]:
    rows = await session.scalars(
        select(ArtifactReleaseSettingsRevision).order_by(
            ArtifactReleaseSettingsRevision.revision.asc()
        )
    )
    return [
        PolicyRevision(
            revision=row.revision,
            embargo_hours=row.embargo_hours,
            disclosure=row.disclosure,
            created_at=_utc(row.created_at) if row.created_at else None,
        )
        for row in rows
    ]


async def load_champions(session: AsyncSession) -> list[ChampionPin]:
    rows = (
        await session.execute(
            select(
                LedgerEpochSnapshot.bench_version,
                LedgerEpochSnapshot.pinned_at,
                LedgerEpochSnapshot.champion_agent_id,
            ).where(LedgerEpochSnapshot.bench_version >= FIRST_BENCH_VERSION)
        )
    ).all()
    return [ChampionPin(int(b), _utc(at), agent_id) for b, at, agent_id in rows]


async def load_public_handoffs(session: AsyncSession) -> dict[UUID, datetime]:
    """First exact-SHA unauthenticated public artifact handoff per agent."""
    rows = (
        await session.execute(
            select(ArtifactFetchAudit.agent_id, func.min(ArtifactFetchAudit.fetched_at))
            .join(Agent, Agent.agent_id == ArtifactFetchAudit.agent_id)
            .where(
                ArtifactFetchAudit.endpoint == ENDPOINT_PUBLIC_ARTIFACT,
                ArtifactFetchAudit.requester_kind == "public",
                ArtifactFetchAudit.requester_id.is_(None),
                ArtifactFetchAudit.artifact_sha256 == Agent.sha256,
            )
            .group_by(ArtifactFetchAudit.agent_id)
        )
    ).all()
    return {agent_id: _utc(at) for agent_id, at in rows}


def release_bench(
    agent_id: UUID, released_at: datetime, facts: ScoreFacts
) -> int | None:
    """The bench a release competes on: the newest it had finalized by then."""
    finalized = {
        a.bench_version: a.finalized_at for a in facts.agents if a.agent_id == agent_id
    }
    benches = sorted(
        {bench for (aid, bench) in facts.composites if aid == agent_id}, reverse=True
    )
    for bench in benches:
        at = finalized.get(bench)
        if at is None or at <= released_at:
            return bench
    return benches[-1] if benches else None


async def fork_candidates(
    session: AsyncSession,
    releases: Sequence[SourceRelease],
    identities: Mapping[UUID, AgentIdentity],
) -> list[ForkCandidate]:
    """Later uploads overlapping each release's distinctive items, or declaring it."""
    if not releases:
        return []
    ids = [r.agent_id for r in releases]
    base_rows = (
        await session.execute(
            select(
                AgentDistinctiveItems.agent_id, AgentDistinctiveItems.item_keys
            ).where(
                AgentDistinctiveItems.agent_id.in_(ids),
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
                AgentDistinctiveItems.error.is_(None),
            )
        )
    ).all()
    base_keys = {agent_id: list(keys) for agent_id, keys in base_rows if keys}
    declared = {
        (parent, agent_id)
        for agent_id, parent in (
            await session.execute(
                select(
                    ScreeningAttempt.agent_id, ScreeningAttempt.lineage_parent_agent_id
                ).where(
                    ScreeningAttempt.lineage_parent_agent_id.in_(ids),
                    ScreeningAttempt.lineage_reference_kind == REFERENCE_RELEASED_BASE,
                )
            )
        ).all()
    }
    measured: dict[tuple[UUID, UUID], tuple[float, float]] = {}
    for release in releases:
        keys = base_keys.get(release.agent_id)
        if not keys:
            continue
        base = cast(keys, ARRAY(BigInteger))
        rows = (
            await session.execute(
                select(
                    AgentDistinctiveItems.agent_id,
                    AgentDistinctiveItems.item_count,
                    func.cardinality(
                        func.array(
                            select(func.unnest(AgentDistinctiveItems.item_keys))
                            .intersect(select(func.unnest(base)))
                            .scalar_subquery()
                        )
                    ),
                )
                .join(Agent, Agent.agent_id == AgentDistinctiveItems.agent_id)
                .where(
                    AgentDistinctiveItems.items_version == ITEMS_VERSION,
                    AgentDistinctiveItems.error.is_(None),
                    AgentDistinctiveItems.agent_id != release.agent_id,
                    AgentDistinctiveItems.item_keys.op("&&")(base),
                    Agent.created_at > release.released_at,
                )
            )
        ).all()
        for agent_id, item_count, shared in rows:
            shared = int(shared or 0)
            novel = (item_count - shared) / item_count if item_count else 0.0
            measured[(release.agent_id, agent_id)] = (
                round(shared / len(keys), 6),
                round(novel, 6),
            )
    out: list[ForkCandidate] = []
    for base_id, fork_id in sorted(set(measured) | declared, key=str):
        identity = identities.get(fork_id)
        if identity is None:
            continue
        containment, novel = measured.get((base_id, fork_id), (None, None))
        out.append(
            ForkCandidate(
                base_agent_id=base_id,
                fork_agent_id=fork_id,
                fork_owner=identity.owner,
                fork_name=identity.name,
                fork_version=identity.version,
                fork_status=identity.status,
                fork_uploaded_at=identity.uploaded_at,
                containment=containment,
                novel_fraction=novel,
                declared=(base_id, fork_id) in declared,
            )
        )
    return out


@dataclass(frozen=True)
class Computation:
    daily: list[DailyRow]
    releases: list[SourceRelease]
    forks: list[ForkRow]
    highs: list[NewHigh]


async def compute(session: AsyncSession, *, now: datetime) -> Computation:
    """Every metric from durable facts, as of ``now``. Read-only."""
    identities = await load_identities(session)
    facts = await load_score_facts(session, identities)
    revisions = await load_revisions(session)
    champions = await load_champions(session)
    handoffs = await load_public_handoffs(session)

    era_start: dict[int, date] = {}
    for agent in facts.agents:
        day = agent.finalized_at.date()
        if agent.bench_version not in era_start or day < era_start[agent.bench_version]:
            era_start[agent.bench_version] = day
    if not era_start:
        return Computation([], [], [], [])
    first_day = min(era_start.values())
    releases: list[SourceRelease] = []
    for agent_id, released_at in handoffs.items():
        identity = identities.get(agent_id)
        if identity is None or released_at.date() < first_day:
            continue
        bench = release_bench(agent_id, released_at, facts)
        policy = policy_at(revisions, released_at, default=DEFAULT_POLICY)
        releases.append(
            SourceRelease(
                agent_id=agent_id,
                owner=identity.owner,
                name=identity.name,
                version=identity.version,
                bench_version=bench,
                released_at=released_at,
                uploaded_at=identity.uploaded_at,
                embargo_hours=policy.embargo_hours,
                disclosure=policy.disclosure,
                base_composite=(
                    facts.composites.get((agent_id, bench))
                    if bench is not None
                    else None
                ),
            )
        )
    candidates = await fork_candidates(session, releases, identities)
    forks = classify_forks(
        releases,
        candidates,
        composites=facts.composites,
        seed_scores=facts.seed_scores,
    )
    owner_first: dict[str, datetime] = {}
    for identity in identities.values():
        held = owner_first.get(identity.owner)
        if held is None or identity.uploaded_at < held:
            owner_first[identity.owner] = identity.uploaded_at
    today = now.date()
    benches = sorted(era_start)
    daily: list[DailyRow] = []
    highs: list[NewHigh] = []
    for index, bench in enumerate(benches):
        agents = [a for a in facts.agents if a.bench_version == bench]
        bench_highs = find_new_highs(
            agents, forks=forks, owner_first_upload=owner_first
        )
        highs.extend(bench_highs)
        last_day = era_start[benches[index + 1]] if index + 1 < len(benches) else today
        daily.extend(
            build_daily(
                bench,
                agents,
                first_day=era_start[bench],
                last_day=min(last_day, today),
                champions=champions,
                releases=releases,
                forks=forks,
                new_highs=bench_highs,
                revisions=revisions,
                default_policy=DEFAULT_POLICY,
            )
        )
    return Computation(daily, releases, forks, highs)


# ----------------------------------------------------------------- serialization


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def high_payload(high: NewHigh) -> dict[str, Any]:
    return {
        "agent_id": str(high.agent_id),
        "name": high.name,
        "version": high.version,
        "bench_version": high.bench_version,
        "finalized_at": _iso(high.finalized_at),
        "composite": high.composite,
        "previous_frontier": high.previous_frontier,
        "delta": high.delta,
        "days_since_previous_high": high.days_since_previous_high,
        "driver": high.driver,
        "base_agent_id": str(high.base_agent_id) if high.base_agent_id else None,
    }


def high_from_payload(payload: Mapping[str, Any]) -> NewHigh:
    return NewHigh(
        agent_id=UUID(payload["agent_id"]),
        name=payload["name"],
        version=payload.get("version"),
        bench_version=int(payload["bench_version"]),
        finalized_at=datetime.fromisoformat(payload["finalized_at"]),
        composite=float(payload["composite"]),
        previous_frontier=float(payload["previous_frontier"]),
        delta=float(payload["delta"]),
        days_since_previous_high=payload.get("days_since_previous_high"),
        driver=payload["driver"],
        base_agent_id=UUID(b) if (b := payload.get("base_agent_id")) else None,
    )


def daily_detail(row: DailyRow) -> dict[str, Any]:
    return {
        "top": [
            {
                "agent_id": str(t.agent_id),
                "name": t.name,
                "version": t.version,
                "composite": t.composite,
            }
            for t in row.top
        ],
        "new_highs": [high_payload(h) for h in row.new_highs],
        "release_agent_ids": [str(a) for a in row.release_agent_ids],
        "fork_agent_ids": [str(a) for a in row.fork_agent_ids],
    }


def daily_from_model(model: CompetitionImprovementDaily) -> DailyRow:
    detail = model.detail or {}
    return DailyRow(
        bench_version=model.bench_version,
        day=model.day,
        frontier_composite=model.frontier_composite,
        frontier_agent_id=model.frontier_agent_id,
        champion_agent_id=model.champion_agent_id,
        champion_composite=model.champion_composite,
        top_n_mean=model.top_n_mean,
        top_n_min=model.top_n_min,
        finalized_agent_count=model.finalized_agent_count,
        new_high_count=model.new_high_count,
        releases_on_day=model.releases_on_day,
        releases_to_date=model.releases_to_date,
        forks_on_day=model.forks_on_day,
        forks_to_date=model.forks_to_date,
        thin_forks_to_date=model.thin_forks_to_date,
        embargo_hours=model.embargo_hours,
        disclosure=model.disclosure,
        top=tuple(
            TopEntry(
                UUID(t["agent_id"]), t["name"], t.get("version"), float(t["composite"])
            )
            for t in detail.get("top", [])
        ),
        new_highs=tuple(high_from_payload(h) for h in detail.get("new_highs", [])),
        release_agent_ids=tuple(UUID(a) for a in detail.get("release_agent_ids", [])),
        fork_agent_ids=tuple(UUID(a) for a in detail.get("fork_agent_ids", [])),
    )


# ----------------------------------------------------------------------- storage


async def store(
    session: AsyncSession, computation: Computation, *, now: datetime
) -> dict[str, int]:
    """Upsert open days, insert missing ones, replace releases and forks.

    A closed row under the current metrics version is left exactly as it was
    written. Releases and forks are event facts recomputed in full each run.
    """
    today = now.date()
    close_before = today - timedelta(days=CLOSE_AFTER_DAYS - 1)
    written = 0
    for row in computation.daily:
        closed = row.day < close_before
        values = {
            "bench_version": row.bench_version,
            "day": row.day,
            "metrics_version": METRICS_VERSION,
            "frontier_composite": row.frontier_composite,
            "frontier_agent_id": row.frontier_agent_id,
            "champion_agent_id": row.champion_agent_id,
            "champion_composite": row.champion_composite,
            "top_n_mean": row.top_n_mean,
            "top_n_min": row.top_n_min,
            "finalized_agent_count": row.finalized_agent_count,
            "new_high_count": row.new_high_count,
            "releases_on_day": row.releases_on_day,
            "releases_to_date": row.releases_to_date,
            "forks_on_day": row.forks_on_day,
            "forks_to_date": row.forks_to_date,
            "thin_forks_to_date": row.thin_forks_to_date,
            "embargo_hours": row.embargo_hours,
            "disclosure": row.disclosure,
            "detail": daily_detail(row),
            "closed": closed,
            "backfilled": closed,
            "computed_at": now,
        }
        statement = insert(CompetitionImprovementDaily).values(**values)
        excluded = statement.excluded
        result = await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    CompetitionImprovementDaily.bench_version,
                    CompetitionImprovementDaily.day,
                ],
                set_={
                    **{
                        k: excluded[k]
                        for k in values
                        if k not in {"bench_version", "day", "backfilled"}
                    },
                    # A row first written while its day was open stays live.
                    "backfilled": CompetitionImprovementDaily.backfilled
                    & excluded.backfilled,
                },
                where=(
                    CompetitionImprovementDaily.closed.is_(False)
                    | (CompetitionImprovementDaily.metrics_version != METRICS_VERSION)
                ),
            )
        )
        written += int(result.rowcount or 0)  # type: ignore[attr-defined]
    await session.execute(delete(CompetitionReleaseFork))
    await session.execute(delete(CompetitionSourceRelease))
    for release in computation.releases:
        session.add(
            CompetitionSourceRelease(
                agent_id=release.agent_id,
                metrics_version=METRICS_VERSION,
                bench_version=release.bench_version,
                released_at=release.released_at,
                uploaded_at=release.uploaded_at,
                embargo_hours=release.embargo_hours,
                disclosure=release.disclosure,
                base_composite=release.base_composite,
                computed_at=now,
            )
        )
    await session.flush()
    for fork in computation.forks:
        session.add(
            CompetitionReleaseFork(
                base_agent_id=fork.base_agent_id,
                fork_agent_id=fork.fork_agent_id,
                metrics_version=METRICS_VERSION,
                bench_version=fork.bench_version,
                released_at=fork.released_at,
                fork_uploaded_at=fork.fork_uploaded_at,
                lag_hours=fork.lag_hours,
                containment=fork.containment,
                novel_fraction=fork.novel_fraction,
                declared=fork.declared,
                thin=fork.thin,
                base_composite=fork.base_composite,
                fork_composite=fork.fork_composite,
                delta=fork.delta,
                shared_seed_count=fork.shared_seed_count,
                shared_seed_delta=fork.shared_seed_delta,
                computed_at=now,
            )
        )
    return {
        "days_computed": len(computation.daily),
        "days_written": written,
        "releases": len(computation.releases),
        "forks": len(computation.forks),
        "new_highs": len(computation.highs),
    }


# -------------------------------------------------------------------------- read


@dataclass(frozen=True)
class StoredMetrics:
    daily: list[DailyRow]
    releases: list[SourceRelease]
    forks: list[ForkRow]
    closed_days: int
    backfilled_days: int
    last_computed_at: datetime | None


async def read_stored(
    session: AsyncSession, *, bench_version: int | None, since: date | None
) -> StoredMetrics:
    where = [CompetitionImprovementDaily.metrics_version == METRICS_VERSION]
    if bench_version is not None:
        where.append(CompetitionImprovementDaily.bench_version == bench_version)
    if since is not None:
        where.append(CompetitionImprovementDaily.day >= since)
    models = list(
        await session.scalars(
            select(CompetitionImprovementDaily)
            .where(*where)
            .order_by(
                CompetitionImprovementDaily.bench_version,
                CompetitionImprovementDaily.day,
            )
        )
    )
    release_rows = (
        await session.execute(
            select(CompetitionSourceRelease, Agent.name, Agent.version)
            .join(Agent, Agent.agent_id == CompetitionSourceRelease.agent_id)
            .where(CompetitionSourceRelease.metrics_version == METRICS_VERSION)
        )
    ).all()
    releases = [
        SourceRelease(
            agent_id=row.agent_id,
            owner="",
            name=name,
            version=version,
            bench_version=row.bench_version,
            released_at=_utc(row.released_at),
            uploaded_at=_utc(row.uploaded_at),
            embargo_hours=row.embargo_hours,
            disclosure=row.disclosure,
            base_composite=row.base_composite,
        )
        for row, name, version in release_rows
        if (bench_version is None or row.bench_version == bench_version)
        and (since is None or _utc(row.released_at).date() >= since)
    ]
    kept = {r.agent_id for r in releases}
    fork_rows = (
        await session.execute(
            select(CompetitionReleaseFork, Agent.name, Agent.version, Agent.status)
            .join(Agent, Agent.agent_id == CompetitionReleaseFork.fork_agent_id)
            .where(CompetitionReleaseFork.metrics_version == METRICS_VERSION)
        )
    ).all()
    forks = [
        ForkRow(
            base_agent_id=row.base_agent_id,
            fork_agent_id=row.fork_agent_id,
            fork_name=name,
            fork_version=version,
            fork_status=_status(status),
            bench_version=row.bench_version,
            released_at=_utc(row.released_at),
            fork_uploaded_at=_utc(row.fork_uploaded_at),
            lag_hours=row.lag_hours,
            containment=row.containment,
            novel_fraction=row.novel_fraction,
            declared=row.declared,
            thin=row.thin,
            base_composite=row.base_composite,
            fork_composite=row.fork_composite,
            delta=row.delta,
            shared_seed_count=row.shared_seed_count,
            shared_seed_delta=row.shared_seed_delta,
        )
        for row, name, version, status in fork_rows
        if row.base_agent_id in kept
    ]
    computed = [m.computed_at for m in models]
    return StoredMetrics(
        daily=[daily_from_model(m) for m in models],
        releases=releases,
        forks=forks,
        closed_days=sum(1 for m in models if m.closed),
        backfilled_days=sum(1 for m in models if m.backfilled),
        last_computed_at=_utc(max(computed)) if computed else None,
    )


async def index_coverage(session: AsyncSession) -> dict[str, int]:
    """How much of the upload history the distinctive-item index covers."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM agents) AS agents,
                  (SELECT count(*) FROM agent_distinctive_items
                    WHERE items_version = :iv AND error IS NULL) AS indexed
                """
            ),
            {"iv": ITEMS_VERSION},
        )
    ).one()
    return {"agents": int(row.agents), "indexed": int(row.indexed)}


async def load_noise_facts(
    session: AsyncSession, *, bench_version: int, top: int
) -> tuple[list[ScoredAgent], ScoreFacts]:
    """The ``top`` scored/live agents of one bench with their per-seed scores."""
    rows = (
        await session.execute(
            select(
                Score.agent_id,
                Score.bench_version,
                Score.composite,
                Score.created_at,
                Score.seed,
                Score.validator_hotkey,
                Agent.name,
                Agent.version,
                Agent.created_at.label("uploaded_at"),
                Agent.status,
            )
            .join(Agent, Agent.agent_id == Score.agent_id)
            .where(
                Score.bench_version == bench_version,
                Agent.status.in_(sorted(FRONTIER_STATUSES)),
            )
        )
    ).all()
    identities = {
        row.agent_id: AgentIdentity(
            agent_id=row.agent_id,
            owner=str(row.agent_id),
            name=row.name,
            version=row.version,
            uploaded_at=_utc(row.uploaded_at),
            status=_status(row.status),
        )
        for row in rows
    }
    folded = fold_scores((tuple(row[:6]) for row in rows), (), identities)
    leaders = sorted(folded.agents, key=lambda a: (-a.composite, a.finalized_at))[:top]
    if not leaders:
        return [], folded
    confirmation = (
        await session.execute(
            select(
                ConfirmationScore.agent_id,
                ConfirmationScore.bench_version,
                ConfirmationScore.seed,
                ConfirmationScore.composite,
            ).where(
                ConfirmationScore.agent_id.in_([a.agent_id for a in leaders]),
                ConfirmationScore.bench_version == bench_version,
                not_voided(),
            )
        )
    ).tuples()
    keep = {a.agent_id for a in leaders}
    facts = fold_scores(
        (tuple(row[:6]) for row in rows if row.agent_id in keep),
        confirmation.all(),
        identities,
    )
    return leaders, facts

"""Operator read of the competition improvement metrics (measurement only).

Reads the rows the hourly job stored plus a live, bounded noise-floor read
of each bench's top agents. Writes nothing.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.competition_improvement import (
    CompetitionBench,
    CompetitionCoverage,
    CompetitionImprovementMetrics,
    CompetitionJobRuntime,
    CompetitionThresholds,
)
from ditto.api_server.competition_improvement import (
    DEFINITIONS,
    FIRST_BENCH_VERSION,
    FORK_CONTAINMENT,
    METRICS_VERSION,
    MIN_NOISE_SEEDS,
    NOISE_AGENTS,
    NOISE_SIGMAS,
    THIN_FORK_NOVEL_FRACTION,
    TOP_N,
    embargo_windows,
    noise_floor,
    summarize_bench,
    summarize_releases,
)
from ditto.api_server.competition_improvement_job import CompetitionImprovementJob
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.competition_improvement import (
    CLOSE_AFTER_DAYS,
    DEFAULT_POLICY,
    index_coverage,
    load_noise_facts,
    load_revisions,
    read_stored,
)

router = APIRouter(prefix="/admin/competition-improvement", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _runtime(request: Request) -> CompetitionJobRuntime | None:
    job = getattr(request.app.state, "competition_improvement_job", None)
    if not isinstance(job, CompetitionImprovementJob):
        return None
    return CompetitionJobRuntime.model_validate(job.runtime())


def _keyed(values: dict[int, float]) -> dict[str, float]:
    return {str(k): v for k, v in values.items()}


@router.get("/metrics", response_model=CompetitionImprovementMetrics)
async def get_competition_improvement_metrics(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
    since: Annotated[date | None, Query()] = None,
) -> CompetitionImprovementMetrics:
    """Frontier, drivers, release forks, embargo windows and noise floor."""
    now = datetime.now(UTC)
    stored = await read_stored(session, bench_version=bench_version, since=since)
    revisions = await load_revisions(session)
    coverage = await index_coverage(session)
    highs = [h for row in stored.daily for h in row.new_highs]
    benches: list[CompetitionBench] = []
    for bench in sorted({row.bench_version for row in stored.daily}):
        leaders, facts = await load_noise_facts(
            session, bench_version=bench, top=NOISE_AGENTS
        )
        noise = noise_floor(
            bench,
            leaders,
            seed_scores=facts.seed_scores,
            quorum=facts.quorum,
        )
        bench_highs = [h for h in highs if h.bench_version == bench]
        summary = summarize_bench(
            bench,
            stored.daily,
            bench_highs,
            now=now,
            beyond_noise_step=noise.beyond_noise_step,
        )
        benches.append(
            CompetitionBench.model_validate(
                {
                    "summary": asdict(summary),
                    "noise": asdict(noise),
                    "new_highs": [asdict(h) for h in bench_highs],
                    "daily": [
                        asdict(row)
                        for row in stored.daily
                        if row.bench_version == bench
                    ],
                }
            )
        )
    releases: list[dict[str, Any]] = []
    for item in summarize_releases(stored.releases, stored.forks):
        release = asdict(item.release)
        release.pop("owner")
        releases.append(
            {
                **release,
                **{
                    k: v
                    for k, v in asdict(item).items()
                    if k not in {"release", "forks"}
                },
                "forks": [asdict(f) for f in item.forks],
            }
        )
    windows = [
        {
            **asdict(window),
            "frontier_gain_by_bench": _keyed(window.frontier_gain_by_bench),
            "gain_per_day_by_bench": _keyed(window.gain_per_day_by_bench),
        }
        for window in embargo_windows(
            revisions,
            default_policy=DEFAULT_POLICY,
            releases=stored.releases,
            forks=stored.forks,
            highs=highs,
            daily=stored.daily,
            now=now,
        )
    ]
    return CompetitionImprovementMetrics.model_validate(
        {
            "metrics_version": METRICS_VERSION,
            "generated_at": now,
            "bench_version": bench_version,
            "since": since,
            "definitions": DEFINITIONS,
            "thresholds": CompetitionThresholds(
                first_bench_version=FIRST_BENCH_VERSION,
                fork_containment=FORK_CONTAINMENT,
                thin_fork_novel_fraction=THIN_FORK_NOVEL_FRACTION,
                top_n=TOP_N,
                noise_agents=NOISE_AGENTS,
                min_noise_seeds=MIN_NOISE_SEEDS,
                noise_sigmas=NOISE_SIGMAS,
                close_after_days=CLOSE_AFTER_DAYS,
            ),
            "coverage": CompetitionCoverage(
                stored_days=len(stored.daily),
                closed_days=stored.closed_days,
                backfilled_days=stored.backfilled_days,
                last_computed_at=stored.last_computed_at,
                agents=coverage["agents"],
                indexed_agents=coverage["indexed"],
            ),
            "runtime": _runtime(request),
            "benches": benches,
            "releases": releases,
            "embargo_windows": windows,
        }
    )

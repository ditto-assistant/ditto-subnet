"""Operator read of the competition improvement metrics (measurement only)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompetitionTopEntry(_Model):
    agent_id: UUID
    name: str
    version: int | None
    composite: float


class CompetitionNewHigh(_Model):
    agent_id: UUID
    name: str
    version: int | None
    bench_version: int
    finalized_at: datetime
    composite: float
    previous_frontier: float
    delta: float
    days_since_previous_high: float | None
    driver: Literal["original", "same_owner_iteration", "released_fork"]
    base_agent_id: UUID | None


class CompetitionDailyRow(_Model):
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
    top: list[CompetitionTopEntry]


class CompetitionBenchSummary(_Model):
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
    largest_steps: list[CompetitionNewHigh]


class CompetitionNoiseAgent(_Model):
    agent_id: UUID
    name: str
    version: int | None
    composite: float
    seed_count: int
    seed_sd: float | None
    quorum_sem: float | None


class CompetitionNoiseFloor(_Model):
    bench_version: int
    agents_measured: int
    median_seed_sd: float | None
    max_seed_sd: float | None
    median_quorum_sem: float | None
    single_seed_delta_sd: float | None
    beyond_noise_step: float | None
    agents: list[CompetitionNoiseAgent]


class CompetitionBench(_Model):
    summary: CompetitionBenchSummary
    noise: CompetitionNoiseFloor
    new_highs: list[CompetitionNewHigh]
    daily: list[CompetitionDailyRow]


class CompetitionFork(_Model):
    fork_agent_id: UUID
    fork_name: str
    fork_version: int | None
    fork_status: str
    bench_version: int | None
    fork_uploaded_at: datetime
    lag_hours: float
    containment: float | None
    novel_fraction: float | None
    declared: bool
    thin: bool
    fork_composite: float | None
    delta: float | None
    shared_seed_count: int
    shared_seed_delta: float | None


class CompetitionRelease(_Model):
    agent_id: UUID
    name: str
    version: int | None
    bench_version: int | None
    uploaded_at: datetime
    released_at: datetime
    embargo_hours: int
    disclosure: str
    base_composite: float | None
    fork_count: int
    thin_fork_count: int
    first_fork_lag_hours: float | None
    median_fork_lag_hours: float | None
    median_novel_fraction: float | None
    median_delta: float | None
    best_delta: float | None
    forks_beating_base: int
    forks: list[CompetitionFork]


class CompetitionEmbargoWindow(_Model):
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
    frontier_gain_by_bench: dict[str, float]
    gain_per_day_by_bench: dict[str, float]


class CompetitionThresholds(_Model):
    first_bench_version: int
    fork_containment: float
    thin_fork_novel_fraction: float
    top_n: int
    noise_agents: int
    min_noise_seeds: int
    noise_sigmas: float
    close_after_days: int


class CompetitionCoverage(_Model):
    stored_days: int
    closed_days: int
    backfilled_days: int
    last_computed_at: datetime | None
    agents: int
    indexed_agents: int


class CompetitionJobRuntime(_Model):
    running: bool
    metrics_version: str
    interval_seconds: float
    last_tick_started_at: datetime | None
    last_tick_finished_at: datetime | None
    last_tick_counters: dict[str, int]
    last_tick_error: str | None


class CompetitionImprovementMetrics(_Model):
    metrics_version: str
    generated_at: datetime
    bench_version: int | None
    since: date | None
    definitions: dict[str, str]
    thresholds: CompetitionThresholds
    coverage: CompetitionCoverage
    runtime: CompetitionJobRuntime | None
    benches: list[CompetitionBench]
    releases: list[CompetitionRelease]
    embargo_windows: list[CompetitionEmbargoWindow]

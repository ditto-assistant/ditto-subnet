"""The measured per-agent evaluation cost published as the readmission cost basis.

SN-485 section 2: the readmission fee equals the upload fee; what Ditto
publishes beside it is the *measured* cost of evaluating one agent on the
active benchmark, never a hardcoded figure. It comes from the Platform
inference ledger the same way the leaderboard's run cost does
(:func:`ditto.api_server.endpoints.public` ``run_costs``): per agent, the mean
chat + embedding cost of its *scored* leases on the current metering contract;
one evaluation is ``SCORING_QUORUM`` runs; the basis is the median across the
agents the leaderboard ranks on the active benchmark (a complete ranked
quorum, :func:`~ditto.db.queries.scores.ranked_quorum_agent_ids`).

It is ``None`` until at least :data:`MIN_AGENTS` agents qualify (grants are
pruned within days, so an old era may have too few), and it is cached in
process for :data:`CACHE_TTL`. Only the public submission terms read it, never
a leaderboard read.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import InferenceGrant, ValidatorTicket
from ditto.db.queries.inference import USAGE_ACCOUNTING_VERSION
from ditto.db.queries.scores import SCORING_QUORUM, ranked_quorum_agent_ids

MIN_AGENTS = 5
CACHE_TTL = timedelta(hours=1)
_MICROUSD_PER_USD = 1_000_000


@dataclass(frozen=True)
class CostBasis:
    bench_version: int
    agents_sampled: int
    runs_per_evaluation: int
    median_run_cost_usd: float | None
    median_agent_cost_usd: float | None
    measured_at: datetime


_cache: dict[int, CostBasis] = {}
_locks: dict[int, asyncio.Lock] = {}
# Tasks holding or queued on each version's lock. ``Lock.locked()`` is false
# while a woken waiter has not yet re-acquired, so eviction counts users
# instead; increments happen before any await, so the count cannot race.
_lock_users: dict[int, int] = {}
# Benchmark versions only advance, so keep the newest few and drop the rest
# rather than holding one entry per version for the process lifetime.
_MAX_CACHED_VERSIONS = 4


def _reset_cache() -> None:
    """Tests only. Locks bind to the loop that first waits on them."""
    _cache.clear()
    _locks.clear()
    _lock_users.clear()


async def _measure(session: AsyncSession, *, bench_version: int) -> CostBasis:
    # The same population the leaderboard ranks: a complete, ranked quorum in
    # the era's season window, scored (not shadow) agents only.
    ranked = await ranked_quorum_agent_ids(session, bench_version=bench_version)
    if not ranked:
        return CostBasis(
            bench_version=bench_version,
            agents_sampled=0,
            runs_per_evaluation=SCORING_QUORUM,
            median_run_cost_usd=None,
            median_agent_cost_usd=None,
            measured_at=datetime.now(UTC),
        )
    rows = (
        await session.execute(
            select(
                InferenceGrant.agent_id,
                func.avg(
                    InferenceGrant.cost_microusd
                    + InferenceGrant.embedding_cost_microusd
                ),
            )
            .join(
                ValidatorTicket,
                and_(
                    InferenceGrant.agent_id == ValidatorTicket.agent_id,
                    InferenceGrant.bench_version == ValidatorTicket.bench_version,
                    InferenceGrant.validator_hotkey == ValidatorTicket.validator_hotkey,
                    InferenceGrant.ticket_deadline == ValidatorTicket.deadline,
                ),
            )
            .where(
                InferenceGrant.agent_id.in_(ranked),
                InferenceGrant.bench_version == bench_version,
                ValidatorTicket.status == TicketStatus.SCORED,
                ValidatorTicket.purpose.not_in(
                    (TicketPurpose.BENCHMARK_CANARY, TicketPurpose.GENERATOR_ROUND)
                ),
                InferenceGrant.usage_accounting_version == USAGE_ACCOUNTING_VERSION,
                or_(
                    InferenceGrant.request_count > 0,
                    InferenceGrant.embedding_request_count > 0,
                ),
            )
            .group_by(InferenceGrant.agent_id)
        )
    ).tuples()
    per_run = [float(average) for _agent, average in rows if average is not None]
    now = datetime.now(UTC)
    if len(per_run) < MIN_AGENTS:
        return CostBasis(
            bench_version=bench_version,
            agents_sampled=len(per_run),
            runs_per_evaluation=SCORING_QUORUM,
            median_run_cost_usd=None,
            median_agent_cost_usd=None,
            measured_at=now,
        )
    median_run = statistics.median(per_run) / _MICROUSD_PER_USD
    return CostBasis(
        bench_version=bench_version,
        agents_sampled=len(per_run),
        runs_per_evaluation=SCORING_QUORUM,
        median_run_cost_usd=round(median_run, 6),
        median_agent_cost_usd=round(median_run * SCORING_QUORUM, 6),
        measured_at=now,
    )


def _fresh(bench_version: int) -> CostBasis | None:
    cached = _cache.get(bench_version)
    if cached is not None and datetime.now(UTC) - cached.measured_at < CACHE_TTL:
        return cached
    return None


async def readmission_cost_basis(
    session: AsyncSession, *, bench_version: int
) -> CostBasis:
    """The cached measured cost basis for ``bench_version``.

    Concurrent misses for one benchmark are serialized, so one request
    measures and the rest reuse its result.
    """
    cached = _fresh(bench_version)
    if cached is not None:
        return cached
    lock = _locks.setdefault(bench_version, asyncio.Lock())
    _lock_users[bench_version] = _lock_users.get(bench_version, 0) + 1
    try:
        async with lock:
            cached = _fresh(bench_version)
            if cached is not None:
                return cached
            measured = await _measure(session, bench_version=bench_version)
            _cache[bench_version] = measured
            return measured
    finally:
        remaining = _lock_users[bench_version] - 1
        if remaining:
            _lock_users[bench_version] = remaining
        else:
            del _lock_users[bench_version]
        _evict_old_versions()


def _evict_old_versions() -> None:
    """Bound ``_cache`` and ``_locks`` to the newest benchmark versions.

    A lock with any holder or queued waiter is kept, so a woken waiter and a
    new caller always share one lock; it is dropped once its last user leaves.
    """
    for version in sorted(_cache)[:-_MAX_CACHED_VERSIONS]:
        del _cache[version]
    keep = set(sorted(_cache)[-_MAX_CACHED_VERSIONS:])
    for version in [v for v in _locks if v not in keep]:
        if not _lock_users.get(version):
            del _locks[version]


__all__ = ["CACHE_TTL", "MIN_AGENTS", "CostBasis", "readmission_cost_basis"]

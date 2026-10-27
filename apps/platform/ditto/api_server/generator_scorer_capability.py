"""The generator scorer's advertised capability, and the guards that use it.

The scorer (``services/dittobench-api/cmd/challenge-scorer``) sends its
version, build revision, sandbox and the case bounds its pack contract accepts
(``challenge.MinCases``/``MaxCases``) as query parameters on every work poll.
Platform keeps the latest per scorer in ``generator_scorer_heartbeats`` and:

* leases no qualification whose ``cases_per_seed`` the polling scorer refuses;
* voids, with an ``infra:`` reason, a round whose ``cases_per_column`` the
  polling scorer refuses (when leased) or the live scorer refuses (at open);
* refuses a settings write whose ``cases_per_seed`` the live scorer refuses,
  unless the operator overrides it.

A scorer older than advertising sends no bounds; Platform then cannot know and
does not refuse (the scorer reports a refused request as a fault instead).

It also advertises, explicitly, the pack grading policies it requests and
grades (``grading_bench_versions``, ``challenge.SupportedGradingBenchVersions``),
the way a validator advertises ``generator-round-bench-<n>``: every component
names the versions it runs and Platform matches them, so upgrades are
asynchronous. A round opens and leases at version N, and a qualification
requests N, only for a scorer listing N. A scorer that lists none (it predates
the list, ``challenge-scorer/5`` and older) is v13-only
(``LEGACY_SCORER_ROUND_BENCH_VERSIONS``), never inferred from its version
string.
On 2026-10-27 a stale scorer (MaxCases 64, cases_per_seed 70) voided round
25968 and rejected a generator; this is the guard that would have stopped it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator_round_validator import (
    LEGACY_SCORER_ROUND_BENCH_VERSIONS,
)
from ditto.db.models import GeneratorScorerHeartbeat

# A scorer seen within this window is "live": its bounds gate settings writes
# and round opens. Polls are every 30s when idle, but one generation or
# qualification can hold the scorer for many minutes.
SCORER_CAPABILITY_TTL = timedelta(hours=6)

Poll = Literal["qualification", "round"]


@dataclass(frozen=True)
class ScorerCapability:
    """What one poll advertised."""

    scorer_id: str
    scorer_version: str
    min_cases: int | None = None
    max_cases: int | None = None
    sandbox: str | None = None
    build_revision: str | None = None
    grading_bench_versions: tuple[int, ...] | None = None
    """Ascending pack grading policies; ``None`` when not advertised."""

    @property
    def known(self) -> bool:
        return self.min_cases is not None and self.max_cases is not None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def refusal(
    scorer: ScorerCapability | GeneratorScorerHeartbeat | None,
    cases: int,
    *,
    what: str = "cases_per_seed",
) -> str | None:
    """Why ``scorer`` cannot run ``cases`` cases per pack, or ``None``.

    ``None`` too when there is no scorer or it advertised no bounds: Platform
    cannot know, so it does not refuse.
    """
    if scorer is None or scorer.min_cases is None or scorer.max_cases is None:
        return None
    if scorer.min_cases <= cases <= scorer.max_cases:
        return None
    return (
        f"generator scorer {scorer.scorer_id} ({scorer.scorer_version}) supports "
        f"{scorer.min_cases}..{scorer.max_cases} cases per pack; {what} is {cases}"
    )


def grading_versions(
    scorer: ScorerCapability | GeneratorScorerHeartbeat | None,
) -> tuple[int, ...]:
    """The pack grading policies ``scorer`` runs: its advertised list, or
    v13 only for a scorer (or a poll) that advertises none."""
    advertised = None if scorer is None else scorer.grading_bench_versions
    if not advertised:
        return LEGACY_SCORER_ROUND_BENCH_VERSIONS
    return tuple(int(v) for v in advertised)


def version_refusal(
    scorer: ScorerCapability | GeneratorScorerHeartbeat | None, bench_version: int
) -> str | None:
    """Why ``scorer`` cannot run a round at ``bench_version``, or ``None``."""
    versions = grading_versions(scorer)
    if bench_version in versions:
        return None
    if scorer is None:
        who = "the generator scorer (none live, or it advertised nothing)"
    else:
        who = f"generator scorer {scorer.scorer_id} ({scorer.scorer_version})"
    listed = ", ".join(f"v{v}" for v in versions)
    note = (
        ""
        if scorer is not None and scorer.grading_bench_versions
        else " (no grading_bench_versions advertised: treated as v13-only)"
    )
    return f"{who} grades {listed}{note}; the round is v{bench_version}"


async def record_heartbeat(
    session: AsyncSession, capability: ScorerCapability, *, poll: Poll, now: datetime
) -> None:
    """Upsert the scorer's latest advertisement (caller commits)."""
    values = {
        "scorer_version": capability.scorer_version,
        "min_cases": capability.min_cases,
        "max_cases": capability.max_cases,
        "sandbox": capability.sandbox,
        "build_revision": capability.build_revision,
        "grading_bench_versions": (
            list(capability.grading_bench_versions)
            if capability.grading_bench_versions
            else None
        ),
        "last_poll": poll,
        "last_seen_at": now,
    }
    statement = insert(GeneratorScorerHeartbeat).values(
        scorer_id=capability.scorer_id, first_seen_at=now, **values
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[GeneratorScorerHeartbeat.scorer_id], set_=values
        )
    )


async def list_heartbeats(session: AsyncSession) -> list[GeneratorScorerHeartbeat]:
    return list(
        await session.scalars(
            select(GeneratorScorerHeartbeat).order_by(
                GeneratorScorerHeartbeat.last_seen_at.desc(),
                GeneratorScorerHeartbeat.scorer_id,
            )
        )
    )


def is_live(row: GeneratorScorerHeartbeat, *, now: datetime) -> bool:
    return _aware(row.last_seen_at) >= now - SCORER_CAPABILITY_TTL


async def live_scorer(
    session: AsyncSession, *, now: datetime
) -> GeneratorScorerHeartbeat | None:
    """The most recently seen scorer inside :data:`SCORER_CAPABILITY_TTL`."""
    return await session.scalar(
        select(GeneratorScorerHeartbeat)
        .where(GeneratorScorerHeartbeat.last_seen_at >= now - SCORER_CAPABILITY_TTL)
        .order_by(GeneratorScorerHeartbeat.last_seen_at.desc())
        .limit(1)
    )

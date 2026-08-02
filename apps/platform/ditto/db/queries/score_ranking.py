"""The one order submissions are ranked in, and the one score that orders them.

Every surface that puts submissions in order reads this module: the public
leaderboard's ``rank``, the validator ledger served to the fleet, the KOTH
champion/tail projection, the score continuation floor and the provisional
contender floor, and the efficiency lineage dedupe. Before this existed each of
those spelled the comparator out again, and two of them disagreed about which
number "the score" is -- which is how a miner came to be told he was below
"fifth place" while the board showed a different agent, with a different number,
at rank 5.

Two things are canonical here and nowhere else:

**The comparator** (:func:`score_order_key`, and its SQL twin
:func:`score_order_terms`) -- ``unranked last, highest score first, earliest
first_seen, lowest agent_id``. The trailing terms are not decoration: they are
what makes a tie deterministic across the API, the queue, and the fold, so two
readers of the same ledger never see two different fifth places.

**The score it orders by** (:func:`official_composites`) -- the KOTH continual
mean. An agent starts on the canonical k=3 quorum median and switches, once it
has retained samples, to the mean of its three signed quorum scores plus one
score per accepted seed. Scheduling membership never removes accepted evidence.
That is the estimator validators fold into
weights, so it is the estimator every ranking surface must cut on, including the
queue floors. See :func:`ditto.api_server.koth.effective_composite` for the
formula itself; this module is only about *who reads it*.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.continual_retest_settings import (
    DEFAULT_WAVE_MEMBERSHIP,
    ContinualRetestSettings,
    WaveMembership,
)
from ditto.score_order import (
    F,
    FinalizedRow,
    RankableRow,
    ScoreOrderKey,
    rank_submissions,
    score_order_key,
    score_order_terms,
)

__all__ = [
    "CONTINUAL_MEAN_PROTOCOL",
    "VALIDATOR_STALE_WINDOW",
    "FinalizedRow",
    "RankableRow",
    "ScoreOrderKey",
    "completed_wave_data",
    "continual_mean_is_active",
    "official_composites",
    "rank_submissions",
    "resolve_official_composites",
    "resolve_ranking_scores",
    "score_order_key",
    "score_order_terms",
]

# Kept in step with ``endpoints/public.py``: the continual mean only becomes the
# official score once every benchmark-capable validator that has been live
# recently speaks the contract that produces it.
CONTINUAL_MEAN_PROTOCOL = 14
VALIDATOR_STALE_WINDOW = timedelta(minutes=15)


def _row_details(row: object) -> dict | None:
    """The row's telemetry blob when it carries one, else ``None``."""
    details = getattr(row, "details", None)
    return details if isinstance(details, dict) else None


def completed_wave_data(
    rows: Sequence[F],
    *,
    stderrs: Mapping[UUID, float | None],
    confirmation_by_seed: Mapping[UUID, Mapping[int, float]] | None = None,
    confirmation_depth: Mapping[UUID, int] | None = None,
    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
    anchor_version: int | None = None,
) -> tuple[list[F], dict[UUID, dict[int, float]], dict[UUID, int]]:
    """Return candidates plus every accepted per-agent confirmation sample.

    ``wave_membership`` and ``anchor_version`` remain accepted for wire/source
    compatibility, but scheduling policy no longer filters score evidence. A
    seed accepted for an agent remains in that agent's mean permanently. Shared
    seed identity is still preserved in ``by_seed`` for the pairwise dethrone
    statistic; it is not a global admission gate.
    """
    del stderrs, wave_membership, anchor_version

    by_seed: dict[UUID, dict[int, float]] = {
        agent_id: dict(values)
        for agent_id, values in (confirmation_by_seed or {}).items()
    }
    depths = dict(confirmation_depth or {})
    candidates = rank_submissions(
        row for row in rows if getattr(row, "eligible", True) and row.composite > 0.0
    )

    depths = dict.fromkeys(depths, 0)
    depths.update({agent_id: len(seeds) for agent_id, seeds in by_seed.items()})
    return candidates, by_seed, depths


def official_composites(
    rows: Iterable[FinalizedRow],
    *,
    quorum: Mapping[UUID, Sequence[float]],
    completed_waves: Mapping[UUID, Mapping[int, float]],
    continual_mean_active: bool,
    efficiency_bonuses: Mapping[UUID, float] | None = None,
    efficiency_fold_active: bool = False,
) -> dict[UUID, float]:
    """The score every ranking surface cuts on, per agent.

    With the continual mean inactive this is the stored quorum median, so the
    whole ranking degrades to the pre-continual behaviour rather than to a
    second, differently-shaped estimator.
    """
    from ditto.api_server.koth import KothEntry, effective_composite

    bonuses = efficiency_bonuses or {}
    if not continual_mean_active and not efficiency_fold_active:
        return {row.agent_id: row.composite for row in rows}
    return {
        row.agent_id: effective_composite(
            KothEntry(
                miner_hotkey=row.miner_hotkey,
                agent_id=row.agent_id,
                composite=row.composite,
                first_seen=row.first_seen,
                raw_rank=0,
                bench_version=row.bench_version,
                quorum_composites=tuple(quorum.get(row.agent_id, ())),
                completed_wave_composites=tuple(
                    value
                    for _seed, value in sorted(
                        completed_waves.get(row.agent_id, {}).items()
                    )
                ),
                efficiency_bonus=(
                    bonuses.get(row.agent_id) if efficiency_fold_active else None
                ),
            )
        )
        for row in rows
    }


async def continual_mean_is_active(
    session: AsyncSession,
    *,
    bench_version: int | None,
    active_version: int | None = None,
    settings: ContinualRetestSettings | None = None,
    fleet_protocol_ready: bool | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether the continual mean is the official score for ``bench_version``.

    Historical eras keep the estimator they were ranked under, so a view pinned
    to anything but the active version always reads the quorum median. The
    optional arguments let a caller that has already resolved the fleet snapshot
    or the cached settings hand them in rather than re-reading them; the
    predicate itself stays in one place either way.
    """
    from ditto.api_server.continual_retest_settings import (
        aggregate_is_active,
        settings_from_row,
    )
    from ditto.db.queries.benchmark_rollout import active_bench_version
    from ditto.db.queries.continual_retest_settings import (
        latest_continual_retest_settings_revision,
    )
    from ditto.db.queries.heartbeats import live_validator_fleet_supports_protocol

    if active_version is None:
        active_version = await active_bench_version(session)
    if bench_version is not None and bench_version != active_version:
        return False
    if settings is None:
        settings = settings_from_row(
            await latest_continual_retest_settings_revision(session)
        )
    if fleet_protocol_ready is None:
        fleet_protocol_ready = await live_validator_fleet_supports_protocol(
            session,
            minimum_protocol=CONTINUAL_MEAN_PROTOCOL,
            bench_version=active_version,
            now=now or datetime.now(UTC),
            freshness=VALIDATOR_STALE_WINDOW,
        )
    return aggregate_is_active(settings, fleet_protocol_ready=fleet_protocol_ready)


async def resolve_official_composites(
    session: AsyncSession,
    *,
    rows: Sequence[F],
    bench_version: int,
    continual_mean_active: bool,
    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
) -> dict[UUID, float]:
    """Read whatever the continual mean needs and return it per agent.

    The public board already holds the quorum pool and the confirmation history
    for other reasons and calls :func:`official_composites` directly with them.
    This is the entry point for callers -- the queue floors -- that hold only
    the ledger, so that they cut on the same number without a second formula.
    """
    from ditto.api_server.endpoints.scoring import _ledger_stderr
    from ditto.db.queries.confirmation_scores import confirmation_composites_by_seed
    from ditto.db.queries.scores import quorum_composites

    if not rows or not continual_mean_active:
        return official_composites(
            rows, quorum={}, completed_waves={}, continual_mean_active=False
        )
    agent_ids = [row.agent_id for row in rows]
    quorum = await quorum_composites(
        session,
        agent_ids,
        bench_versions={row.agent_id: row.bench_version for row in rows},
    )
    confirmation_by_seed = await confirmation_composites_by_seed(
        session, agent_ids=agent_ids, bench_version=bench_version
    )
    stderrs = {
        row.agent_id: _ledger_stderr(_row_details(row), quorum.get(row.agent_id, []))
        for row in rows
    }
    _candidates, completed_by_seed, _depths = completed_wave_data(
        rows,
        stderrs=stderrs,
        confirmation_by_seed=confirmation_by_seed,
        wave_membership=wave_membership,
    )
    return official_composites(
        rows,
        quorum=quorum,
        completed_waves=completed_by_seed,
        continual_mean_active=True,
    )


async def resolve_ranking_scores(
    session: AsyncSession,
    *,
    rows: Sequence[F],
    bench_version: int | None,
    now: datetime | None = None,
) -> dict[UUID, float]:
    """The canonical score for every row, settings and all, from a bare session.

    The board resolves the continual-retest settings through the app's cached
    resolver and threads them by hand. A caller with only a session -- the queue
    floors -- would otherwise have to restate that resolution, and a floor
    computed under a different ``wave_membership`` than the board is exactly the
    kind of near-miss disagreement this module exists to stop. So the settings
    are read once here and drive both the activation predicate and the fold's
    membership mode.
    """
    from ditto.api_server.continual_retest_settings import settings_from_row
    from ditto.db.queries.benchmark_rollout import active_bench_version
    from ditto.db.queries.continual_retest_settings import (
        latest_continual_retest_settings_revision,
    )

    settings = settings_from_row(
        await latest_continual_retest_settings_revision(session)
    )
    active_version = await active_bench_version(session)
    active = await continual_mean_is_active(
        session,
        bench_version=bench_version,
        active_version=active_version,
        settings=settings,
        now=now,
    )
    era = bench_version if bench_version is not None else active_version
    if era is None:
        return official_composites(
            rows, quorum={}, completed_waves={}, continual_mean_active=False
        )
    return await resolve_official_composites(
        session,
        rows=rows,
        bench_version=era,
        continual_mean_active=active,
        wave_membership=settings.wave_membership,
    )

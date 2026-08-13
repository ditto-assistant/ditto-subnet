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

**The score it orders by** (:func:`official_composites`) -- the authoritative
quality scalar followed by any activated Platform adjustment. Legacy eras start
on the canonical k=3 quorum median and may switch to the continual mean. A
full-confirmed Bench-v9 row instead uses its independently verified full quality;
curve-v3 efficiency, when activated, multiplies downside and applies upside to
the remaining quality headroom. This is the estimator validators
fold into weights, so every ranking surface must cut on it, including queue
floors. See
:func:`ditto.api_server.koth.effective_composite` for the legacy continual
formula; this module is only about *who reads it*.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime, timedelta
from math import exp
from typing import TYPE_CHECKING, cast
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

if TYPE_CHECKING:
    from ditto.api_server.config import EfficiencyBonusConfig

__all__ = [
    "CONTINUAL_MEAN_PROTOCOL",
    "EfficiencyFactorRequesterNotReady",
    "VALIDATOR_STALE_WINDOW",
    "FinalizedRow",
    "RankableRow",
    "ScoreOrderKey",
    "completed_wave_data",
    "continual_mean_is_active",
    "dedupe_owner_rows",
    "official_composites",
    "rank_submissions",
    "resolve_efficiency_adjustments",
    "resolve_official_composites",
    "resolve_ranking_scores",
    "score_order_key",
    "score_order_terms",
]

# Kept in step with ``endpoints/public.py``: the continual mean only becomes the
# official score once every benchmark-capable validator that has been live
# recently speaks the contract that produces it.
CONTINUAL_MEAN_PROTOCOL = 14
BOUNDED_EFFICIENCY_FACTOR_PROTOCOL = 19
VALIDATOR_STALE_WINDOW = timedelta(minutes=15)


class EfficiencyFactorRequesterNotReady(RuntimeError):
    """The ledger requester lacks fresh heartbeat membership for curve v3.

    This is raised only after a bounded-factor assignment is found. Legacy
    ledgers and v9 epochs without factor candidates do not require requester
    heartbeat evidence merely to read their existing scoring authority.
    """


def _row_details(row: object) -> dict | None:
    """The row's telemetry blob when it carries one, else ``None``."""
    details = getattr(row, "details", None)
    return details if isinstance(details, dict) else None


def dedupe_owner_rows(rows: Sequence[F], *, scores: Mapping[UUID, float]) -> list[F]:
    """Select one official representative per payment/attestation owner.

    Callers pass the exact score their surface ranks—full-v9 quality followed
    by the activated factor—so a leaner equal-quality generation wins before
    first-seen. Rows without an internal owner root fall back to a unique agent
    key, preserving fixtures and historical value objects.
    """
    by_owner: dict[str, list[F]] = {}
    for row in rows:
        owner = getattr(row, "emission_owner_root", None) or f"agent:{row.agent_id}"
        by_owner.setdefault(str(owner), []).append(row)
    winners = [
        _with_resolved_crown(
            rank_submissions(group, scores=scores)[0], group=group, scores=scores
        )
        for group in by_owner.values()
    ]
    return rank_submissions(winners, scores=scores)


def _with_resolved_crown(
    winner: F, *, group: Sequence[F], scores: Mapping[UUID, float]
) -> F:
    """Re-anchor owner seniority on the same final score that chose its row.

    PostgreSQL computes ``crown_first_seen`` before Platform efficiency is
    available. Once a factor changes which generation represents an owner, the
    earlier raw-quality anchor is no longer authoritative: an expensive early
    sibling must not lend seniority to a later efficient winner unless their
    *final* scores are still inside the flat dethrone band. Ledger rows are
    frozen dataclasses, so replace only that derived field; lightweight test or
    moderation rows without it retain their ordinary upload timestamp.
    """
    if not is_dataclass(winner) or not hasattr(winner, "crown_first_seen"):
        return winner

    from ditto.api_server.koth import (
        KOTH_BAND_DECAY_MIN_BENCH_VERSION,
        KOTH_BAND_DECAY_RATE,
        KOTH_BAND_DECAY_START_COMPOSITE,
        KOTH_MARGIN,
    )

    winner_score = scores.get(winner.agent_id, winner.composite)
    scale = 1.0
    if winner.bench_version >= KOTH_BAND_DECAY_MIN_BENCH_VERSION:
        bounded = min(max(winner_score, KOTH_BAND_DECAY_START_COMPOSITE), 1.0)
        scale = exp(-KOTH_BAND_DECAY_RATE * (bounded - KOTH_BAND_DECAY_START_COMPOSITE))
    threshold = winner_score - KOTH_MARGIN * scale
    ancestors = [
        row.first_seen
        for row in group
        if bool(getattr(row, "eligible", True))
        and row.bench_version == winner.bench_version
        and scores.get(row.agent_id, row.composite) >= threshold
    ]
    anchor = min(ancestors, default=winner.first_seen)
    return cast(F, replace(winner, crown_first_seen=anchor))


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
    efficiency_factors: Mapping[UUID, float] | None = None,
    efficiency_fold_active: bool = False,
) -> dict[UUID, float]:
    """The score every ranking surface cuts on, per agent.

    With the continual mean inactive this is the stored quorum median except
    for full-confirmed Bench-v9 rows, whose verified full composite remains
    authoritative. Curve-v3 factors are accepted only for those v9 rows and
    only while the coordinated efficiency fold is active.
    """
    from ditto.api_server.koth import KothEntry, effective_composite

    bonuses = efficiency_bonuses or {}
    factors = efficiency_factors or {}

    def authoritative_quality(row: FinalizedRow) -> float:
        """The quality scalar efficiency multiplies for this benchmark era."""
        confirmation = getattr(row, "v9_confirmation", None)
        if row.bench_version == 9 and isinstance(confirmation, Mapping):
            micros = confirmation.get("full_effective_micros")
            if (
                isinstance(micros, int)
                and not isinstance(micros, bool)
                and 0 <= micros <= 1_000_000
            ):
                return micros / 1_000_000
        return row.composite

    if not continual_mean_active and not efficiency_fold_active:
        return {row.agent_id: authoritative_quality(row) for row in rows}
    return {
        row.agent_id: effective_composite(
            KothEntry(
                miner_hotkey=row.miner_hotkey,
                agent_id=row.agent_id,
                composite=authoritative_quality(row),
                first_seen=row.first_seen,
                raw_rank=0,
                bench_version=row.bench_version,
                quorum_composites=(
                    ()
                    if row.bench_version == 9
                    and getattr(row, "v9_confirmation", None) is not None
                    else tuple(quorum.get(row.agent_id, ()))
                ),
                completed_wave_composites=tuple(
                    value
                    for _seed, value in sorted(
                        (
                            {}
                            if row.bench_version == 9
                            and getattr(row, "v9_confirmation", None) is not None
                            else completed_waves.get(row.agent_id, {})
                        ).items()
                    )
                ),
                efficiency_bonus=(
                    bonuses.get(row.agent_id) if efficiency_fold_active else None
                ),
                efficiency_factor=(
                    factors.get(row.agent_id)
                    if efficiency_fold_active
                    and row.bench_version == 9
                    and getattr(row, "v9_confirmation", None) is not None
                    else None
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
    efficiency_config: EfficiencyBonusConfig | None = None,
    now: datetime | None = None,
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

    if not rows:
        return {}

    bonuses, factors = await resolve_efficiency_adjustments(
        session,
        rows=rows,
        efficiency_config=efficiency_config,
        now=now,
    )

    adjustment_active = bool(bonuses or factors)
    if not continual_mean_active:
        return official_composites(
            rows,
            quorum={},
            completed_waves={},
            continual_mean_active=False,
            efficiency_bonuses=bonuses,
            efficiency_factors=factors,
            efficiency_fold_active=adjustment_active,
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
        efficiency_bonuses=bonuses,
        efficiency_factors=factors,
        efficiency_fold_active=adjustment_active,
    )


async def resolve_efficiency_adjustments(
    session: AsyncSession,
    *,
    rows: Sequence[F],
    efficiency_config: EfficiencyBonusConfig | None = None,
    now: datetime | None = None,
    requesting_validator_hotkey: str | None = None,
) -> tuple[dict[UUID, float], dict[UUID, float]]:
    """Resolve the fleet-safe adjustments shared by every official fold.

    The scoring ledger, queue floors, and Platform KOTH scheduler must not each
    interpret assignment rows independently. This keeps the policy, epoch,
    v9/full-confirmation eligibility, and protocol-19 gate identical across
    those authority paths.

    App callers pass the resolver's effective config so an env-seeded rollout
    is honored. Bare DB callers read the latest persisted revision; with no
    revision the historical default-off policy remains in force.
    """
    from ditto.api_server.config import EfficiencyBonusConfig
    from ditto.api_server.efficiency import epoch_index_for
    from ditto.api_server.efficiency_settings import effective_config, settings_from_row
    from ditto.db.models import ValidatorHeartbeat
    from ditto.db.queries.efficiency import get_bonus_rows
    from ditto.db.queries.efficiency_settings import (
        latest_efficiency_settings_revision,
    )
    from ditto.db.queries.heartbeats import live_weight_setter_fleet_supports_protocol

    if not rows:
        return {}, {}
    if efficiency_config is None:
        revision = await latest_efficiency_settings_revision(session)
        settings = settings_from_row(revision)
        efficiency_config = effective_config(EfficiencyBonusConfig(), settings)
    if not efficiency_config.enabled or not efficiency_config.fold_enabled:
        return {}, {}

    resolved_now = now or datetime.now(UTC)
    assignments = await get_bonus_rows(
        session,
        [row.agent_id for row in rows],
        bench_versions={row.agent_id: row.bench_version for row in rows},
        epoch_index=epoch_index_for(resolved_now, efficiency_config.epoch_hours),
    )
    by_id = {row.agent_id: row for row in rows}
    bonuses = {
        agent_id: assignment.bonus
        for agent_id, assignment in assignments.items()
        if assignment.factor is None and agent_id in by_id
    }
    factor_candidates = {
        agent_id: float(assignment.factor)
        for agent_id, assignment in assignments.items()
        if assignment.factor is not None
        and agent_id in by_id
        and by_id[agent_id].bench_version == 9
        and getattr(by_id[agent_id], "v9_confirmation", None) is not None
    }
    factors: dict[UUID, float] = {}
    if factor_candidates and requesting_validator_hotkey is not None:
        requester = await session.get(ValidatorHeartbeat, requesting_validator_hotkey)
        if (
            requester is None
            or requester.seen_at < resolved_now - VALIDATOR_STALE_WINDOW
        ):
            # A requester absent from the fresh fleet would escape the global
            # protocol minimum and could consume a factor that its local fold
            # does not understand. A fresh pre-v19 requester, by contrast, is
            # deliberately admitted: its heartbeat remains in the global query
            # below and neutralizes factors for every validator atomically.
            raise EfficiencyFactorRequesterNotReady(
                "a fresh validator heartbeat is required before serving "
                "bounded efficiency factors"
            )
    if factor_candidates and await live_weight_setter_fleet_supports_protocol(
        session,
        minimum_protocol=BOUNDED_EFFICIENCY_FACTOR_PROTOCOL,
        now=resolved_now,
        freshness=VALIDATOR_STALE_WINDOW,
    ):
        factors = factor_candidates
    return bonuses, factors


async def resolve_ranking_scores(
    session: AsyncSession,
    *,
    rows: Sequence[F],
    bench_version: int | None,
    efficiency_config: EfficiencyBonusConfig | None = None,
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
        efficiency_config=efficiency_config,
        now=now,
    )

"""Reads + append-only writes for the top-5 shared-seed confirmation ledger.

The continual top-5 rescore lane (``docs/top5-rescore-lane.md``) accumulates one
immutable :class:`~ditto.db.models.ConfirmationScore` row per
``(agent_id, validator_hotkey, bench_version, seed)``. Writes are
INSERT-idempotent (``ON CONFLICT DO NOTHING``) and never UPDATE/delete, so the
record grows monotonically over a champion's reign and stays fully auditable.

The KOTH fold reads paired evidence from this history: per agent, the per-seed
composite is the **median across validators** (N-agnostic, like the k=3
composite), and the fold pairs a challenger against the champion on their shared
seeds. This module returns those per-seed aggregates plus the shared-seed depth
surfaced on the leaderboard.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ditto.api_models.continual_retest_settings import (
    DEFAULT_WAVE_MEMBERSHIP,
    WaveMembership,
)
from ditto.db.models import ConfirmationScore

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ConfirmationSeedScore:
    """One validator's immutable composite for one champion-anchored seed."""

    agent_id: UUID
    validator_hotkey: str
    seed: int
    composite: float
    run_id: str
    signature: str | None
    v9_efficiency_token_total: int | None = None
    """Signed single-seed v9 token cost, available from protocol 19 onward."""
    v9_efficiency_cost_eligible: bool | None = None
    """``True`` for valid cost evidence, ``False`` for an explicit integrity
    failure, and ``None`` for historical/non-v9 rows without cost authority."""


@dataclass(frozen=True)
class ConfirmationEfficiencyCosts:
    """Retest observations available to one agent's daily cost average."""

    seed_token_totals: tuple[float, ...] = ()
    """One entry per retested seed, ascending by seed: the median audited cost
    across the validators that scored it. Seeds, not rows, are the unit of
    observation — the same rule :func:`confirmation_composites_by_seed` applies
    to quality, so a seed several validators happened to draw does not outweigh
    one a single validator drew, and no lone validator sets a seed's cost."""
    evidence_valid: bool = True


@dataclass(frozen=True)
class ConfirmationHistoryRow:
    """One append-only confirmation record as exposed on the ledger read.

    Raw per-``(validator_hotkey, seed)`` rows (NOT pre-aggregated): the KOTH fold
    groups them by seed and medians the composite across validators itself.
    """

    agent_id: UUID
    seed: int
    composite: float
    validator_hotkey: str
    bench_version: int
    signature: str | None


def completed_confirmation_wave_seeds(
    *,
    member_ids: Iterable[UUID],
    seeds_by_agent: Mapping[UUID, Iterable[int]],
) -> frozenset[int]:
    """Seeds with accepted confirmation evidence for every cohort member.

    Continual retests are cohort waves, not independent per-agent samples.  A
    partially completed wave stays append-only and visible for audit, but must
    not enter the KOTH fold until every current top-five member has one result
    for the same seed.  Otherwise the first report can change the champion and
    invalidate the still-running leases for the rest of the wave.

    This is the ``strict`` half of :func:`fold_eligible_seeds_by_agent`; see
    there for why "every current member" is not the same predicate as "every
    member the wave was actually issued to".
    """
    members = tuple(dict.fromkeys(member_ids))
    if not members:
        return frozenset()
    common: set[int] | None = None
    for member_id in members:
        member_seeds = set(seeds_by_agent.get(member_id, ()))
        common = member_seeds if common is None else common & member_seeds
        if not common:
            return frozenset()
    return frozenset(common or ())


def confirmation_catchup_seeds(
    *,
    member_id: UUID,
    peer_ids: Iterable[UUID],
    anchored_seeds: Sequence[int],
    seeds_by_agent: Mapping[UUID, Iterable[int]],
) -> tuple[int, ...]:
    """The member's whole backlog: seeds ANY peer holds and it does not.

    This is the *coverage gap* of one member measured against the rest of its
    wave, in ``anchored_seeds`` order. A seed nobody has reached yet is ordinary
    wave growth, paced by :func:`completed_confirmation_wave_seeds`, and a member
    must never run ahead of the wave. A seed some peer already holds is different
    in kind -- it is recorded evidence this member lacks, so there is no wave
    left to tear and no ordering to preserve. Every one of them can be leased at
    once.

    That distinction is the whole point. A member arriving at seed depth zero
    needs, under one-seed-per-round pacing, one round per anchored seed to
    converge -- during which the intersection that gates the KOTH fold stays
    empty and every sibling's accumulated depth stops counting (see
    :func:`fold_eligible_seeds_by_agent`). Returning the backlog in full lets the
    fleet drain it in one round instead of N, so the degraded window is brief
    rather than chronic.

    ANY peer, not every peer. This was the intersection over peers, which
    quietly made the backlog only as wide as the peers' *common* coverage: a
    seed that one peer held and the others did not was invisible to catch-up,
    so nobody was ever sent to it and it stayed a permanent orphan. It could
    not enter the fold either, because the fold intersects over members and
    that seed was missing from most of them. Runs that had already been paid for
    therefore sat in the table contributing to nothing -- 188 of 288 recorded
    runs on the 2026-07-28 board.

    Reading the union instead turns every one of those into a lease target: one
    member holding a seed is sufficient reason to send everybody else to it, and
    once they arrive the seed enters the intersection and starts counting. That
    is also what makes a dethrone survivable. The anchor is keyed on the
    champion's agent id, so a new crown introduces a disjoint seed set; under the
    intersection reading the outgoing reign's coverage was abandoned, while under
    the union reading it is simply backlog the cohort converges onto. The shared
    set then grows monotonically across reigns instead of resetting, which is
    what the champion-anchored design assumed all along and only holds if nothing
    is discarded.

    Returns ``()`` when there are no peers: a lone member has nothing to catch
    up to, and growth pacing owns the decision.
    """
    peers = tuple(
        peer_id for peer_id in dict.fromkeys(peer_ids) if peer_id != member_id
    )
    if not peers:
        return ()
    reached: set[int] = set()
    for peer_id in peers:
        reached.update(seeds_by_agent.get(peer_id, ()))
    if not reached:
        return ()
    held = frozenset(seeds_by_agent.get(member_id, ()))
    return tuple(
        seed for seed in anchored_seeds if seed in reached and seed not in held
    )


async def lane_seed_universe(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> tuple[int, ...]:
    """Every seed the lane has already recorded for these agents, seed-ordered.

    The champion-anchored anchor describes one reign: at most
    ``TOP5_MAX_CONFIRMATION_SEEDS`` seeds, disjoint from the previous crown's.
    Bounding the lane to it means a dethrone abandons whatever coverage the
    outgoing reign accumulated, which is how an agent ends up holding 53
    recorded seeds against a 16-seed cap with none of them foldable.

    The universe is the cumulative set instead: the anchor says which seeds are
    NEW, and this says which are already in play. Callers bound catch-up and the
    fold to ``anchor | universe`` so growth keeps introducing unpredictable
    seeds -- that is the anti-overfit property the champion key provides, and it
    is preserved exactly -- while nothing already paid for falls out of scope.

    Sorted rather than anchor-ordered because a cumulative set has no single
    issuing order to inherit; callers put the live anchor first and append this,
    so growth pacing still walks the current reign in its own order. Deterministic
    either way, which is what the per-seed lease fairness check needs.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return ()
    rows = await session.execute(
        select(ConfirmationScore.seed)
        .where(
            ConfirmationScore.agent_id.in_(ids),
            ConfirmationScore.bench_version == bench_version,
        )
        .distinct()
    )
    return tuple(sorted(int(seed) for (seed,) in rows))


def fold_eligible_seeds_by_agent(
    *,
    member_ids: Iterable[UUID],
    seeds_by_agent: Mapping[UUID, Iterable[int]],
    mode: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
    anchored_seeds: Sequence[int] | None = None,
) -> dict[UUID, frozenset[int]]:
    """Per agent, which confirmation seeds may enter the fold's aggregate.

    Three policies, widening in order. All three are pure functions of the same
    two inputs; the mode is an operator setting because the choice changes what
    ``effective_composite`` averages and therefore what validators weight.

    ``strict`` -- the historical behaviour, and now the ROLLBACK PATH rather
        than the default. One global intersection over EVERY current
        emission-set member, handed to every agent.

        The invariant it buys is real: ``effective_composite`` averages the three
        quorum scores together with one score per wave, and it is handed a bare
        tuple of composites with the seed identity already discarded. Comparing
        two such means is only sound when the two agents were measured on the
        SAME seeds, because seed difficulty is the dominant variance term --
        that is the whole reason the lane uses common random numbers.

        Its defect is that "every current member" is evaluated against the live
        emission set rather than against the membership the wave was issued to.
        A newly finalized agent entering the top five with no retests of its own
        empties the intersection, and every other agent's accumulated depth
        silently stops counting. This is not display-only: ``official_composite``
        falls back from the continual mean to the three-score quorum median, so
        the aggregate feeding validator weights reverts too. Every top-five
        membership change currently discards the fold's accumulated evidence.

    ``participants`` -- **the shipped default.** The same intersection, taken
        over members that hold at
        least one confirmation row. An agent that has never reported a single
        seed is not "still running" any wave, so it cannot be protecting a lease;
        including it can only erase evidence, never validate any. Equal sample
        composition is fully preserved among every agent that receives a
        continual mean, because they still share one intersected seed set. A
        member at depth zero simply keeps its quorum median until it starts
        reporting, exactly as every agent outside the emission set already does.

        This is a strictly smaller change than it looks: the board ALREADY mixes
        the two estimators. ``official_composite`` is computed for every
        finalized row and sorted into one list, so an agent with completed waves
        is already ranked directly against an agent on its quorum median at the
        rank-five boundary. ``participants`` does not introduce that mixing; it
        stops a membership change from flipping every agent onto the noisier
        estimator at once.

    ``per_agent`` -- each agent aggregates over its own completed seeds, with no
        intersection at all. This is the most responsive and the least
        comparable: two agents' means are then taken over different seed sets, so
        the difference between them carries a seed-composition term that the
        shared-seed design exists to cancel. Under exchangeable CRN draws it
        stays unbiased, but it adds variance, and variance is what the whole lane
        was built to suppress -- at a ``KOTH_MARGIN`` of 0.007 the noise is the
        same size as the decision. Offered because it is what "retests should
        count for an agent even when others lack waves" literally asks for, and
        gated because it changes what validators weight.

    Note that the paired dethrone test is untouched by all three.
    ``koth._paired_statistic`` computes its OWN pairwise seed intersection
    between challenger and champion, so the crown comparison stays paired
    whatever this returns.

    ``anchored_seeds`` scopes every mode to ONE champion's anchor, and is what
    makes the three policies above mean what they claim on a board that churns.
    The anchor is a pure function of the champion's agent id
    (:func:`ditto.api_server.crn.champion_anchored_seeds`), so a dethrone
    replaces the seed set wholesale -- two champions' anchors are disjoint, and
    only ``TOP5_MAX_CONFIRMATION_SEEDS`` of them exist per reign. The
    append-only trail, however, spans every anchor the agent has ever been
    rescored under. Intersecting those raw trails therefore compares seeds from
    different reigns, and it fails in the direction that costs the most: an
    agent carrying deep history from a PREVIOUS champion is admitted by the
    ``participants`` predicate on the strength of rows that cannot match the
    current anchor, and then empties the intersection for everyone. That is the
    same defect ``participants`` was introduced to fix, arriving from the
    opposite side -- there a member at depth zero erased the wave, here a member
    at depth thirty-nine does.

    Filtering to the anchor FIRST fixes both halves at once: the predicate then
    asks "is this agent participating in the CURRENT wave", which is the
    question it was always meant to ask, and the intersection is taken over
    seeds that are actually comparable. ``None`` keeps the unscoped behaviour
    for callers that have no champion to anchor on (and for the rollback path).
    """
    members = tuple(dict.fromkeys(member_ids))
    anchor = None if anchored_seeds is None else frozenset(anchored_seeds)
    own = {
        agent_id: (frozenset(seeds) if anchor is None else frozenset(seeds) & anchor)
        for agent_id, seeds in seeds_by_agent.items()
    }
    if mode == "per_agent":
        return own
    if mode == "participants":
        members = tuple(member_id for member_id in members if own.get(member_id))
        # ...and of those, only the ones that overlap SOMEBODY. Holding rows is
        # not the same as being in the shared wave. A member whose coverage is
        # disjoint from every other member's contributes nothing the
        # intersection could keep and can only empty it -- which is precisely
        # what a previous reign's survivor did on the 2026-07-28 board, vetoing
        # every sibling's accumulated depth on the strength of seeds nobody else
        # had ever been scored on.
        #
        # This is the same judgement the depth-zero exclusion already makes, one
        # step further along: an agent that has not joined the wave does not get
        # to hold it closed. It is deliberately "overlaps somebody" and not
        # "holds a current-anchor seed" -- the latter empties the fold outright
        # for the whole window after a dethrone, when the new anchor is in
        # nobody's history yet and the cohort's real shared evidence is entirely
        # cross-reign.
        overlapping: list[UUID] = []
        for member_id in members:
            others: set[int] = set()
            for other_id in members:
                if other_id != member_id:
                    others.update(own.get(other_id, frozenset()))
            if own[member_id] & others:
                overlapping.append(member_id)
        if overlapping:
            members = tuple(overlapping)
    # Over ``own``, not the caller's raw mapping: the intersection has to see the
    # same anchor-scoped sets the predicate just ran on, or a stale-anchor seed
    # could still slip into ``shared`` and be handed back as fold-eligible.
    shared = completed_confirmation_wave_seeds(member_ids=members, seeds_by_agent=own)
    # Intersected with what the agent actually holds, so the result is that
    # agent's fold-eligible seeds rather than a filter to be applied later. For
    # every member this is the shared set unchanged (the intersection could not
    # contain a seed the member is missing); it only narrows for a non-member
    # that happens to carry rows, which the widened retest cohort makes routine.
    return {agent_id: seeds & shared for agent_id, seeds in own.items()}


async def append_confirmation_scores(
    session: AsyncSession,
    *,
    rows: Sequence[ConfirmationSeedScore],
    bench_version: int,
    created_at: datetime,
) -> int:
    """Append confirmation rows idempotently; return the count actually inserted.

    ``ON CONFLICT DO NOTHING`` on the ``(agent_id, bench_version,
    validator_hotkey, seed)`` key: a re-submitted seed (the validator resends the
    whole champion-anchored union each round) is a no-op, and the first-written
    composite for a ``(validator, seed)`` is immutable. Because dittobench is
    deterministic per ``(agent, seed)`` a re-score would produce the identical
    composite, so idempotency is consensus-safe.
    """
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    values = [
        {
            "agent_id": row.agent_id,
            "validator_hotkey": row.validator_hotkey,
            "bench_version": bench_version,
            "seed": row.seed,
            "composite": row.composite,
            "run_id": row.run_id,
            "signature": row.signature,
            "v9_efficiency_token_total": row.v9_efficiency_token_total,
            "v9_efficiency_cost_eligible": row.v9_efficiency_cost_eligible,
            "created_at": created_at,
        }
        for row in rows
    ]
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    statement: Any = (
        insert(ConfirmationScore)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=["agent_id", "bench_version", "validator_hotkey", "seed"]
        )
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


async def confirmation_efficiency_costs_by_agent(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[UUID, ConfirmationEfficiencyCosts]:
    """Return protocol-19 continual-retest costs for the efficiency snapshot.

    Historical rows carry a null eligibility marker and are ignored because
    their token telemetry was not bound into the confirmation signature. Every
    new single-seed v9 retest carries either a positive authoritative total or
    an explicit invalid marker. One invalid marker fails that agent's daily
    average closed instead of silently dropping an inconvenient run.

    Rows collapse to one observation per seed by median across validators,
    matching :func:`confirmation_composites_by_seed`. Averaging raw rows would
    weight a seed by how many validators happened to draw it and would let a
    lone validator set that seed's cost, neither of which is true of the
    quality this cost is ranked beside.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ConfirmationScore.agent_id,
            ConfirmationScore.seed,
            ConfirmationScore.v9_efficiency_token_total,
            ConfirmationScore.v9_efficiency_cost_eligible,
        )
        .where(
            ConfirmationScore.agent_id.in_(ids),
            ConfirmationScore.bench_version == bench_version,
        )
        .order_by(
            ConfirmationScore.agent_id,
            ConfirmationScore.seed,
            ConfirmationScore.validator_hotkey,
        )
    )
    by_seed: dict[UUID, dict[int, list[int]]] = {}
    validity = dict.fromkeys(ids, True)
    for agent_id, seed, token_total, eligible in rows:
        if eligible is None:
            continue
        if eligible is not True or token_total is None or token_total <= 0:
            validity[agent_id] = False
            continue
        by_seed.setdefault(agent_id, {}).setdefault(seed, []).append(int(token_total))
    return {
        agent_id: ConfirmationEfficiencyCosts(
            seed_token_totals=tuple(
                float(statistics.median(totals))
                for _seed, totals in sorted(by_seed.get(agent_id, {}).items())
            ),
            evidence_valid=validity[agent_id],
        )
        for agent_id in ids
    }


async def confirmation_composites_by_seed(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[UUID, dict[int, float]]:
    """Per agent, ``{seed: median composite across validators}`` for one version.

    The median across validators mirrors the k=3 composite selection (no single
    validator decides a seed), and pairs the fold uses shared seeds against the
    champion. Absent agents / versions map to an empty dict.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ConfirmationScore.agent_id,
            ConfirmationScore.seed,
            ConfirmationScore.composite,
        ).where(
            ConfirmationScore.agent_id.in_(ids),
            ConfirmationScore.bench_version == bench_version,
        )
    )
    grouped: dict[UUID, dict[int, list[float]]] = {}
    for agent_id, seed, composite in rows:
        grouped.setdefault(agent_id, {}).setdefault(seed, []).append(composite)
    return {
        agent_id: {
            seed: statistics.median(composites) for seed, composites in seeds.items()
        }
        for agent_id, seeds in grouped.items()
    }


async def confirmation_history_by_agent(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[UUID, list[ConfirmationHistoryRow]]:
    """Per agent, the raw append-only confirmation rows for one version.

    Ordered ``(seed, validator_hotkey)`` for a deterministic wire order. Raw
    per-``(validator, seed)`` records so the fold does its own group-by-seed
    median; the platform does not pre-aggregate the exposed history.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ConfirmationScore.agent_id,
            ConfirmationScore.seed,
            ConfirmationScore.composite,
            ConfirmationScore.validator_hotkey,
            ConfirmationScore.bench_version,
            ConfirmationScore.signature,
        )
        .where(
            ConfirmationScore.agent_id.in_(ids),
            ConfirmationScore.bench_version == bench_version,
        )
        .order_by(ConfirmationScore.seed, ConfirmationScore.validator_hotkey)
    )
    history: dict[UUID, list[ConfirmationHistoryRow]] = {}
    for agent_id, seed, composite, validator_hotkey, version, signature in rows:
        history.setdefault(agent_id, []).append(
            ConfirmationHistoryRow(
                agent_id=agent_id,
                seed=seed,
                composite=composite,
                validator_hotkey=validator_hotkey,
                bench_version=version,
                signature=signature,
            )
        )
    return history


async def confirmation_depths(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[UUID, int]:
    """Per agent, the shared-seed confirmation depth = number of distinct seeds.

    This is the "N shared-seed confirmations" count surfaced on the leaderboard;
    it grows while an agent holds its emission-set spot.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(
            ConfirmationScore.agent_id,
            func.count(func.distinct(ConfirmationScore.seed)),
        )
        .where(
            ConfirmationScore.agent_id.in_(ids),
            ConfirmationScore.bench_version == bench_version,
        )
        .group_by(ConfirmationScore.agent_id)
    )
    return {agent_id: int(depth) for agent_id, depth in rows}

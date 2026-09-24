"""Authenticated leaderboard projection that keeps stored agent names.

Public ``GET /public/leaderboard`` strikes colliding handles after an upheld
name claim. Operators still need the stored ``agents.name`` for audit, so
Backroom reads this route instead of the cacheable public board.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.continual_retest_diagnostic import (
    AdminContinualRetestDiagnostic,
    RetestClaimability,
    RetestCutoffComparison,
    RetestFamilyMember,
)
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.public import PublicLeaderboardResponse
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.confirmation_seed_anchor import read_reign_seed_anchor
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import (
    _LEADERBOARD_DETAIL_FIELDS,
    build_public_leaderboard,
)
from ditto.api_server.endpoints.validator import (
    ChainDep,
    SessionDep,
    _canonical_tail_is_draining,
    _claimable_confirmation_seed,
    _configured_retest_cohort,
    _current_koth_entries,
    _current_retest_cohort,
    _live_retest_leases,
    _retest_cohort_cutoff,
    _top5_confirmation_seed_plan,
    _top5_member_is_least_covered,
    _unserved_catchup_members,
)
from ditto.api_server.koth import (
    KothEntry,
    effective_composite,
    project_koth,
    tie_band_comparison,
    top5_round_is_due,
)
from ditto.chain import ChainClient
from ditto.db.models import Agent, ConfirmationScore, ValidatorTicket
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_scores import confirmation_composites_by_seed
from ditto.db.queries.queue_order import miner_has_newer_canonical_work

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]

_NO_VALIDATOR = ""
"""A hotkey no validator can hold.

The claimability read has to answer "could the fleet lease this agent now",
not "could validator X", and it must not look like a poll from anybody. An
empty hotkey owns no ticket and no retest event, so every per-validator
predicate resolves to the fleet-wide answer.
"""


@router.get(
    "/leaderboard",
    response_model=PublicLeaderboardResponse,
    response_model_exclude={"entries": {"__all__": _LEADERBOARD_DETAIL_FIELDS}},
)
async def admin_leaderboard(
    request: Request,
    response: Response,
    session: SessionDep,
    _admin: AdminDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
) -> PublicLeaderboardResponse:
    """Same ledger as the public board, with stored agent names."""
    board = await build_public_leaderboard(
        request,
        response,
        session,
        bench_version,
        strike_colliding_names=False,
    )
    response.headers["Cache-Control"] = "no-store"
    return board


_ClaimDecision = Literal[
    "claimable",
    "lane_disabled",
    "not_in_cohort",
    "chain_unavailable",
    "round_not_due",
    "newer_canonical_work_pending",
    "no_pending_seeds",
    "all_pending_seeds_leased",
    "another_member_less_covered",
]
_AdmissionReason = Literal[
    "in_cohort",
    "same_owner_challenger",
    "owner_suppressed",
    "outside_cohort",
    "not_current_finalized_ledger",
]
_RepresentativeSelection = Literal[
    "self",
    "official_composite",
    "efficiency_tiebreak",
    "newest_generation",
    "agent_id_tiebreak",
    "none",
]
_RoutePriority = Literal["champion", "catchup", "emission", "extended", "not_routed"]


def _claim_decision(
    *,
    lane_enabled: bool,
    in_cohort: bool,
    scheduled_round: bool | None,
    spare_capacity: bool,
    idle_retests_enabled: bool,
    in_catchup: bool,
    pending_seed_count: int,
    claimable_seed_available: bool,
    newer_canonical_work_pending: bool,
    least_covered_admitted: bool | None,
) -> _ClaimDecision:
    """The first reason the issuance lane would decline this exact agent.

    Evaluated in the lane's own order, so the answer is the gate a validator
    polling right now would actually hit rather than a list of everything that
    happens to be false. ``scheduled_round`` is ``None`` only when the chain
    read failed, which leaves every cadence gate below it unresolved.
    """
    if not lane_enabled:
        return "lane_disabled"
    if not in_cohort:
        return "not_in_cohort"
    if scheduled_round is None:
        return "chain_unavailable"
    if (
        not scheduled_round
        and not spare_capacity
        and not idle_retests_enabled
        and not in_catchup
    ):
        return "round_not_due"
    if not scheduled_round and newer_canonical_work_pending:
        return "newer_canonical_work_pending"
    if pending_seed_count == 0:
        return "no_pending_seeds"
    if not claimable_seed_available:
        return "all_pending_seeds_leased"
    if least_covered_admitted is False:
        return "another_member_less_covered"
    return "claimable"


def _cutoff_comparison(
    entry: KothEntry | None,
    cutoff: KothEntry | None,
    *,
    tolerance_z: float,
    official_scores: dict[UUID, float],
) -> RetestCutoffComparison:
    """Measure one agent against one cutoff with the fold's own tie band."""
    if cutoff is None:
        return RetestCutoffComparison()
    composite = official_scores.get(cutoff.agent_id)
    if entry is None:
        return RetestCutoffComparison(agent_id=cutoff.agent_id, composite=composite)
    band = tie_band_comparison(entry, cutoff, tolerance_z=tolerance_z)
    return RetestCutoffComparison(
        agent_id=cutoff.agent_id,
        composite=composite,
        gap=band.gap,
        tie_band=band.tolerance,
        within_tie_band=band.indistinguishable,
    )


def _representative_selection(
    *,
    agent_id: UUID,
    representative_id: UUID | None,
    official_scores: dict[UUID, float],
    entries_by_id: dict[UUID, KothEntry],
) -> _RepresentativeSelection:
    """Which term of the owner-family key actually decided the representative.

    Mirrors :func:`ditto.score_order.owner_family_key`: official quality first,
    the curve-v3 efficiency secondary next, then the newest lineage clock, then
    the stable agent-id tiebreak.
    """
    if representative_id is None:
        return "none"
    if representative_id == agent_id:
        return "self"
    own = official_scores.get(agent_id)
    representative = official_scores.get(representative_id)
    if own is None or representative is None or own != representative:
        return "official_composite"
    own_entry = entries_by_id.get(agent_id)
    representative_entry = entries_by_id.get(representative_id)
    if own_entry is None or representative_entry is None:
        return "official_composite"
    if effective_composite(own_entry) != effective_composite(representative_entry):
        return "efficiency_tiebreak"
    if own_entry.first_seen != representative_entry.first_seen:
        return "newest_generation"
    return "agent_id_tiebreak"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _claimability(
    session: AsyncSession,
    *,
    request: Request,
    agent: Agent,
    agent_id: UUID,
    version: int,
    settings: ContinualRetestSettings,
    now: datetime,
    chain: ChainClient,
    emission: tuple[KothEntry, ...],
    wave: tuple[KothEntry, ...],
    cohort: tuple[KothEntry, ...],
    challenger_ids: frozenset[UUID],
    in_cohort: bool,
) -> RetestClaimability:
    """Project the issuance lane's gates onto one agent without claiming work.

    Every predicate is the lane's own function, queried for an empty validator
    hotkey (:data:`_NO_VALIDATOR`) so the answer is fleet-wide and no live
    lease, retest event, or slot is attributed to a real validator. Outstanding
    work is counted, never enumerated: the seed values are private challenge
    material and a diagnostic has no reason to hold them.
    """
    config = request.app.state.config
    lane_enabled = config.top5_backoff_base > 0
    # Same two sets, and the same conflation, the lane applies: seed planning
    # follows the public board while the folded set still belongs in the wave.
    wave_member_ids = tuple(
        dict.fromkeys(
            (
                *(member.agent_id for member in wave),
                *(member.agent_id for member in emission),
            )
        )
    )
    emission_member_ids = frozenset(wave_member_ids)
    member_ids = tuple(member.agent_id for member in cohort)
    champion_agent_id = (
        wave[0].agent_id
        if wave
        else emission[0].agent_id
        if emission
        else cohort[0].agent_id
        if cohort
        else None
    )
    latest_block: int | None = None
    try:
        latest_block = (await chain.get_latest_block()).number
    except Exception:
        logger.warning("retest diagnostic chain read failed", exc_info=True)
    champion = (
        await session.get(Agent, champion_agent_id)
        if champion_agent_id is not None
        else None
    )
    crown_block = (
        champion.dataset_seed_block if champion is not None else None
    ) or latest_block
    scheduled_round = (
        top5_round_is_due(
            latest_block,
            crown_block,
            base=config.top5_backoff_base,
            doubling_k=config.top5_backoff_doubling_tempos,
            cap=config.top5_backoff_cap,
        )
        if lane_enabled and latest_block is not None and crown_block is not None
        else None
    )
    spare_capacity = await _canonical_tail_is_draining(
        session,
        requesting_validator=_NO_VALIDATOR,
        canonical_version=version,
        now=now,
    )
    catchup_ids = (
        await _unserved_catchup_members(
            session,
            champion_agent_id=champion_agent_id,
            wave_member_ids=wave_member_ids,
            emission_member_ids=tuple(member.agent_id for member in emission),
            challenger_member_ids=tuple(challenger_ids),
            canonical_version=version,
            now=now,
        )
        if champion_agent_id is not None
        else frozenset()
    )
    plan = (
        await _top5_confirmation_seed_plan(
            session,
            champion_agent_id=champion_agent_id,
            member_agent_id=agent_id,
            wave_member_ids=wave_member_ids,
            cohort_member_ids=member_ids,
            canonical_version=version,
        )
        if champion_agent_id is not None
        else ()
    )
    leases = (
        await _live_retest_leases(
            session,
            member_agent_ids=(agent_id,),
            canonical_version=version,
            now=now,
        )
    ).get(agent_id, {})
    claimable = _claimable_confirmation_seed(
        seeds=plan, leases=leases, validator_hotkey=_NO_VALIDATOR
    )
    newer_canonical_work_pending = await miner_has_newer_canonical_work(
        session,
        miner_hotkey=agent.miner_hotkey,
        created_before=agent.created_at,
        bench_version=version,
    )
    least_covered = (
        await _top5_member_is_least_covered(
            session,
            members=tuple(dict.fromkeys((*wave, *emission, *cohort))),
            emission_member_ids=emission_member_ids,
            catchup_member_ids=catchup_ids,
            requested_member_id=agent_id,
            wave_seed=claimable,
            validator_hotkey=_NO_VALIDATOR,
            canonical_version=version,
            now=now,
        )
        if in_cohort and claimable is not None
        else None
    )
    candidate_ids: tuple[UUID, ...] = ()
    if champion_agent_id is not None:
        rest_ids = tuple(
            member_id for member_id in member_ids if member_id != champion_agent_id
        )
        candidate_ids = (
            champion_agent_id,
            *(member_id for member_id in rest_ids if member_id in catchup_ids),
            *(
                member_id
                for member_id in rest_ids
                if member_id in emission_member_ids and member_id not in catchup_ids
            ),
            *(
                member_id
                for member_id in rest_ids
                if member_id not in emission_member_ids and member_id not in catchup_ids
            ),
        )
    route_priority: _RoutePriority = (
        "champion"
        if agent_id == champion_agent_id
        else "catchup"
        if agent_id in catchup_ids
        else "emission"
        if agent_id in emission_member_ids
        else "extended"
        if agent_id in member_ids
        else "not_routed"
    )
    return RetestClaimability(
        lane_enabled=lane_enabled,
        latest_block=latest_block,
        champion_agent_id=champion_agent_id,
        champion_crown_block=(
            champion.dataset_seed_block if champion is not None else None
        ),
        scheduled_round=scheduled_round,
        spare_capacity_window=spare_capacity,
        idle_retests_enabled=settings.idle_retests_enabled,
        in_catchup_set=agent_id in catchup_ids,
        route_priority=route_priority,
        route_position=(
            candidate_ids.index(agent_id) + 1 if agent_id in candidate_ids else None
        ),
        pending_seed_count=len(plan),
        claimable_seed_available=claimable is not None,
        live_lease_count=len(leases),
        newer_canonical_work_pending=newer_canonical_work_pending,
        least_covered_admitted=least_covered,
        decision=_claim_decision(
            lane_enabled=lane_enabled,
            in_cohort=in_cohort,
            scheduled_round=scheduled_round,
            spare_capacity=spare_capacity,
            idle_retests_enabled=settings.idle_retests_enabled,
            in_catchup=agent_id in catchup_ids,
            pending_seed_count=len(plan),
            claimable_seed_available=claimable is not None,
            newer_canonical_work_pending=newer_canonical_work_pending,
            least_covered_admitted=least_covered,
        ),
    )


@router.get(
    "/agents/{agent_id}/continual-retest-diagnostic",
    response_model=AdminContinualRetestDiagnostic,
)
async def continual_retest_diagnostic(
    request: Request,
    response: Response,
    session: SessionDep,
    chain: ChainDep,
    _admin: AdminDep,
    agent_id: UUID,
) -> AdminContinualRetestDiagnostic:
    """Explain one UUID's current admission with the scheduler's own fold.

    This reads accepted score evidence and current policy. It cannot issue a
    ticket, override an exclusion, or change the leaderboard. Confirmation
    datasets, prompts, and answer keys are never returned, and outstanding work
    is reported as a count rather than a seed list.
    """
    response.headers["Cache-Control"] = "no-store"
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="unknown agent id")
    now = datetime.now(UTC)
    version = await active_bench_version(session)
    settings = await request.app.state.continual_retest_settings.resolve(
        request.app.state.session_maker
    )
    efficiency = await request.app.state.efficiency_settings.resolve(
        request.app.state.session_maker
    )
    snapshot = await _current_koth_entries(
        session,
        canonical_version=version,
        wave_membership=settings.wave_membership,
        efficiency_config=efficiency,
        now=now,
    )
    emission, wave, cohort, challenger_ids = await _current_retest_cohort(
        session,
        canonical_version=version,
        settings=settings,
        efficiency_config=efficiency,
        now=now,
        snapshot=snapshot,
    )
    entries_by_id = {entry.agent_id: entry for entry in snapshot.all_entries}
    entry = entries_by_id.get(agent_id)
    owner = snapshot.owner_by_agent.get(agent_id)
    representative_id = snapshot.owner_representatives.get(owner) if owner else None
    family_ids = (
        sorted(
            (
                other_id
                for other_id, root in snapshot.owner_by_agent.items()
                if root == owner
            ),
            key=lambda other_id: (
                -snapshot.canonical_scores.get(other_id, 0.0),
                str(other_id),
            ),
        )
        if owner
        else []
    )
    family = [
        RetestFamilyMember(
            agent_id=other_id,
            canonical_composite=snapshot.canonical_scores[other_id],
            official_composite=snapshot.official_scores[other_id],
            representative=other_id == representative_id,
            effective_composite=(
                effective_composite(entries_by_id[other_id])
                if other_id in entries_by_id
                else None
            ),
            canonical_sample_count=(
                len(entries_by_id[other_id].quorum_composites or ())
                if other_id in entries_by_id
                else 0
            ),
            completed_wave_depth=len(snapshot.folded_seeds_by_agent.get(other_id, ())),
            first_seen=(
                entries_by_id[other_id].first_seen
                if other_id in entries_by_id
                else None
            ),
        )
        for other_id in family_ids
    ]
    raw = await confirmation_composites_by_seed(
        session, agent_ids=[agent_id], bench_version=version
    )
    ticket_rows = await session.execute(
        select(
            ValidatorTicket.status,
            ValidatorTicket.deadline,
            ValidatorTicket.updated_at,
            ValidatorTicket.validator_hotkey,
            ValidatorTicket.failure_reason,
        ).where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.bench_version == version,
            ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
        )
    )
    ticket_counts: dict[str, int] = {}
    active_ticket_count = 0
    terminal_ticket_count = 0
    latest_ticket: tuple[TicketStatus, datetime | None, str, str | None] | None = None
    for status, deadline, updated_at, validator_hotkey, failure_reason in ticket_rows:
        ticket_counts[status.value] = ticket_counts.get(status.value, 0) + 1
        if status == TicketStatus.ISSUED and deadline > now:
            active_ticket_count += 1
        else:
            # A lease past its deadline is no longer live, and the ticket row a
            # reviewer wants is the newest settled attempt.
            terminal_ticket_count += 1
        stamp = _as_utc(updated_at)
        if (
            latest_ticket is None
            or latest_ticket[1] is None
            or (stamp is not None and stamp > latest_ticket[1])
        ):
            latest_ticket = (status, stamp, validator_hotkey, failure_reason)
    latest_confirmation = next(
        iter(
            await session.execute(
                select(ConfirmationScore.composite, ConfirmationScore.created_at)
                .where(
                    ConfirmationScore.agent_id == agent_id,
                    ConfirmationScore.bench_version == version,
                )
                .order_by(ConfirmationScore.created_at.desc())
                .limit(1)
            )
        ),
        None,
    )
    anchor_champion_id = wave[0].agent_id if wave else None
    anchor = (
        await read_reign_seed_anchor(
            session, champion_agent_id=anchor_champion_id, bench_version=version
        )
        if anchor_champion_id is not None
        else None
    )
    cohort_ids = {member.agent_id for member in cohort}
    in_cohort = agent_id in cohort_ids
    challenger = agent_id in challenger_ids
    reason: _AdmissionReason = (
        "same_owner_challenger"
        if challenger
        else "in_cohort"
        if in_cohort
        else "owner_suppressed"
        if owner and representative_id != agent_id
        else "outside_cohort"
        if agent_id in snapshot.official_scores
        else "not_current_finalized_ledger"
    )
    # The cutoff an operator is shown has to be the cutoff the lane applied, so
    # both come from one builder over the same folded pool. A suppressed
    # generation is absent from that pool entirely -- which is exactly why the
    # cohort cutoff can exclude it without any band being evaluated against it.
    configured_cohort = _configured_retest_cohort(
        snapshot.folded_entries,
        project_koth(snapshot.folded_entries),
        settings=settings,
    )
    statistical = settings.retest_eligibility_mode == "statistical"
    claim = await _claimability(
        session,
        request=request,
        agent=agent,
        agent_id=agent_id,
        version=version,
        settings=settings,
        now=now,
        chain=chain,
        emission=emission,
        wave=wave,
        cohort=cohort,
        challenger_ids=challenger_ids,
        in_cohort=in_cohort,
    )
    return AdminContinualRetestDiagnostic(
        generated_at=now,
        agent_id=agent_id,
        agent_status=agent.status.value,
        active_bench_version=version,
        canonical_composite=snapshot.canonical_scores.get(agent_id),
        official_composite=snapshot.official_scores.get(agent_id),
        owner_representative_id=representative_id,
        family=family,
        raw_confirmation_seeds=[str(seed) for seed in sorted(raw.get(agent_id, {}))],
        folded_confirmation_seeds=[
            str(seed) for seed in snapshot.folded_seeds_by_agent.get(agent_id, ())
        ],
        in_raw_wave=agent_id in {member.agent_id for member in wave},
        in_emission_set=agent_id in {member.agent_id for member in emission},
        in_retest_cohort=in_cohort,
        is_same_owner_challenger=challenger,
        cohort_position=next(
            (
                index
                for index, member in enumerate(cohort, start=1)
                if member.agent_id == agent_id
            ),
            None,
        ),
        cohort_size=len(cohort),
        configured_cohort_size=settings.retest_cohort_size,
        eligibility_mode=settings.retest_eligibility_mode,
        eligibility_z=settings.retest_eligibility_z,
        configured_max_size=settings.retest_cohort_max_size,
        ticket_status_counts=ticket_counts,
        active_ticket_count=active_ticket_count,
        seed_anchor_champion_id=anchor_champion_id,
        seed_anchor_block=anchor.anchor_block if anchor is not None else None,
        seed_anchor_pinned=anchor.pinned if anchor is not None else None,
        admission_reason=reason,
        ledger_eligible=agent_id in snapshot.official_scores,
        canonical_sample_count=(
            len(entry.quorum_composites or ()) if entry is not None else 0
        ),
        completed_wave_depth=len(snapshot.folded_seeds_by_agent.get(agent_id, ())),
        official_sample_count=(
            len(entry.quorum_composites or ())
            + len(snapshot.folded_seeds_by_agent.get(agent_id, ()))
            if entry is not None
            else 0
        ),
        raw_confirmation_depth=len(raw.get(agent_id, {})),
        composite_stderr=entry.composite_stderr if entry is not None else None,
        aggregate_mode=settings.aggregate_mode,
        wave_membership=settings.wave_membership,
        owner_key=owner,
        representative_canonical_composite=(
            snapshot.canonical_scores.get(representative_id)
            if representative_id is not None
            else None
        ),
        representative_official_composite=(
            snapshot.official_scores.get(representative_id)
            if representative_id is not None
            else None
        ),
        representative_margin=(
            snapshot.official_scores[representative_id]
            - snapshot.official_scores[agent_id]
            if representative_id in snapshot.official_scores
            and agent_id in snapshot.official_scores
            else None
        ),
        representative_selection=_representative_selection(
            agent_id=agent_id,
            representative_id=representative_id,
            official_scores=snapshot.official_scores,
            entries_by_id=entries_by_id,
        ),
        cohort_cutoff=_cutoff_comparison(
            entry,
            _retest_cohort_cutoff(configured_cohort, settings=settings),
            tolerance_z=settings.retest_eligibility_z if statistical else 0.0,
            official_scores=snapshot.official_scores,
        ),
        emission_cutoff=_cutoff_comparison(
            entry,
            emission[-1] if emission else None,
            tolerance_z=0.0,
            official_scores=snapshot.official_scores,
        ),
        claim=claim,
        latest_ticket_status=(
            latest_ticket[0].value if latest_ticket is not None else None
        ),
        latest_ticket_validator_hotkey=(
            latest_ticket[2] if latest_ticket is not None else None
        ),
        latest_ticket_updated_at=(
            latest_ticket[1] if latest_ticket is not None else None
        ),
        latest_ticket_failure_reason=(
            latest_ticket[3] if latest_ticket is not None else None
        ),
        terminal_ticket_count=terminal_ticket_count,
        latest_confirmation_composite=(
            latest_confirmation[0] if latest_confirmation is not None else None
        ),
        latest_confirmation_recorded_at=(
            _as_utc(latest_confirmation[1]) if latest_confirmation is not None else None
        ),
    )

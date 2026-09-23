"""Authenticated leaderboard projection that keeps stored agent names.

Public ``GET /public/leaderboard`` strikes colliding handles after an upheld
name claim. Operators still need the stored ``agents.name`` for audit, so
Backroom reads this route instead of the cacheable public board.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select

from ditto.api_models.continual_retest_diagnostic import (
    AdminContinualRetestDiagnostic,
    RetestFamilyMember,
)
from ditto.api_models.public import PublicLeaderboardResponse
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.confirmation_seed_anchor import read_reign_seed_anchor
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import (
    _LEADERBOARD_DETAIL_FIELDS,
    build_public_leaderboard,
)
from ditto.api_server.endpoints.validator import (
    SessionDep,
    _current_koth_entries,
    _current_retest_cohort,
)
from ditto.db.models import Agent, ValidatorTicket
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_scores import confirmation_composites_by_seed

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


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


@router.get(
    "/agents/{agent_id}/continual-retest-diagnostic",
    response_model=AdminContinualRetestDiagnostic,
)
async def continual_retest_diagnostic(
    request: Request,
    response: Response,
    session: SessionDep,
    _admin: AdminDep,
    agent_id: UUID,
) -> AdminContinualRetestDiagnostic:
    """Explain one UUID's current admission with the scheduler's own fold.

    This reads accepted score evidence and current policy. It cannot issue a
    ticket, override an exclusion, or change the leaderboard.
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
        )
        for other_id in family_ids
    ]
    raw = await confirmation_composites_by_seed(
        session, agent_ids=[agent_id], bench_version=version
    )
    ticket_rows = await session.execute(
        select(ValidatorTicket.status, ValidatorTicket.deadline).where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.bench_version == version,
            ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
        )
    )
    ticket_counts: dict[str, int] = {}
    active_ticket_count = 0
    for status, deadline in ticket_rows:
        ticket_counts[status.value] = ticket_counts.get(status.value, 0) + 1
        if status == TicketStatus.ISSUED and deadline > now:
            active_ticket_count += 1
    anchor_champion_id = wave[0].agent_id if wave else None
    anchor = (
        await read_reign_seed_anchor(
            session, champion_agent_id=anchor_champion_id, bench_version=version
        )
        if anchor_champion_id is not None
        else None
    )
    cohort_ids = {entry.agent_id for entry in cohort}
    in_cohort = agent_id in cohort_ids
    challenger = agent_id in challenger_ids
    reason: Literal[
        "in_cohort",
        "same_owner_challenger",
        "owner_suppressed",
        "outside_cohort",
        "not_current_finalized_ledger",
    ] = (
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
        in_raw_wave=agent_id in {entry.agent_id for entry in wave},
        in_emission_set=agent_id in {entry.agent_id for entry in emission},
        in_retest_cohort=in_cohort,
        is_same_owner_challenger=challenger,
        cohort_position=next(
            (
                index
                for index, entry in enumerate(cohort, start=1)
                if entry.agent_id == agent_id
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
    )

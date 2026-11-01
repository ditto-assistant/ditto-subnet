"""Operator reads and guarded writes for continual-retest infrastructure voids."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.confirmation_score_voids import (
    VOID_CONFIRMATION_PHRASE,
    InfraVoidList,
    InfraVoidOutcome,
    InfraVoidRecord,
    InfraVoidRequest,
    InfraVoidResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import ConfirmationScore
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_score_voids import (
    VoidOutcome,
    VoidRequest,
    list_confirmation_score_voids,
    void_confirmation_samples,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _require_actor(x_admin_actor: str | None) -> str:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


@router.post("/confirmation-score-voids", response_model=InfraVoidResponse)
async def void_infra_confirmation_scores(
    payload: InfraVoidRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> InfraVoidResponse:
    """Void continual-retest samples whose own grant shows an inference outage.

    Each item is guarded on its exact run id and composite and gated on the
    serving grant's outage-class failures. ``dry_run`` (the default) writes
    nothing. A voided sample counts toward nothing and its seed is re-leased;
    the score row itself is never changed.
    """
    actor = _require_actor(x_admin_actor)
    if not payload.dry_run and payload.confirmation != VOID_CONFIRMATION_PHRASE:
        raise HTTPException(
            status_code=422,
            detail=f"confirmation must equal {VOID_CONFIRMATION_PHRASE!r}",
        )
    seen = {(item.agent_id, item.validator_hotkey, item.seed) for item in payload.items}
    if len(seen) != len(payload.items):
        raise HTTPException(status_code=422, detail="duplicate void item")
    now = datetime.now(UTC)
    try:
        async with session.begin():
            version, outcomes = await _apply(session, payload, actor=actor, now=now)
    except _BatchRefused as refused:
        # All or nothing: the transaction rolled back; re-read and re-send.
        return InfraVoidResponse(
            bench_version=refused.version,
            dry_run=False,
            applied_count=0,
            outcomes=[_outcome_wire(o, applied=False) for o in refused.outcomes],
        )
    return InfraVoidResponse(
        bench_version=version,
        dry_run=payload.dry_run,
        applied_count=sum(o.disposition == "applied" for o in outcomes),
        outcomes=[_outcome_wire(outcome, applied=True) for outcome in outcomes],
    )


class _BatchRefused(Exception):
    def __init__(self, version: int, outcomes: list[VoidOutcome]) -> None:
        super().__init__("void batch refused")
        self.version = version
        self.outcomes = outcomes


async def _apply(
    session: AsyncSession, payload: InfraVoidRequest, *, actor: str, now: datetime
) -> tuple[int, list[VoidOutcome]]:
    version = await active_bench_version(session)
    outcomes = await void_confirmation_samples(
        session,
        requests=[
            VoidRequest(
                agent_id=item.agent_id,
                validator_hotkey=item.validator_hotkey,
                seed=int(item.seed),
                expected_run_id=item.expected_run_id,
                expected_composite=item.expected_composite,
            )
            for item in payload.items
        ],
        bench_version=version,
        reason=payload.reason,
        actor=actor,
        source="operator",
        now=now,
        apply=not payload.dry_run,
    )
    if not payload.dry_run and any(
        outcome.disposition not in {"applied", "already_voided"} for outcome in outcomes
    ):
        raise _BatchRefused(version, outcomes)
    return version, outcomes


def _outcome_wire(outcome: VoidOutcome, *, applied: bool) -> InfraVoidOutcome:
    disposition = outcome.disposition
    if disposition == "applied" and not applied:
        disposition = "ready"
    return InfraVoidOutcome(
        agent_id=outcome.request.agent_id,
        validator_hotkey=outcome.request.validator_hotkey,
        seed=str(outcome.request.seed),
        disposition=disposition,
        detail=outcome.detail,
        void_id=outcome.void_id if applied else None,
        void_generation=outcome.void_generation,
        evidence=outcome.evidence,
        evidence_digest=outcome.evidence_digest,
    )


@router.get("/confirmation-score-voids", response_model=InfraVoidList)
async def list_infra_confirmation_voids(
    _admin: AdminDep,
    session: SessionDep,
    agent_id: Annotated[UUID | None, Query()] = None,
    source: Annotated[Literal["operator", "outage_sweep"] | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InfraVoidList:
    """Every infrastructure void, newest first, with the re-run once it lands."""
    voids = await list_confirmation_score_voids(
        session,
        agent_id=agent_id,
        source=source,
        since=since,
        limit=limit,
        offset=offset,
    )
    reruns: dict[tuple[UUID, int, str, int, int], tuple[float, str]] = {}
    for void in voids:
        rerun = (
            await session.execute(
                select(ConfirmationScore.composite, ConfirmationScore.run_id).where(
                    ConfirmationScore.agent_id == void.agent_id,
                    ConfirmationScore.bench_version == void.bench_version,
                    ConfirmationScore.validator_hotkey == void.validator_hotkey,
                    ConfirmationScore.seed == void.seed,
                    ConfirmationScore.void_generation == void.void_generation + 1,
                )
            )
        ).first()
        if rerun is not None:
            reruns[
                (
                    void.agent_id,
                    void.bench_version,
                    void.validator_hotkey,
                    void.seed,
                    void.void_generation,
                )
            ] = (float(rerun[0]), str(rerun[1]))
    items = []
    for void in voids:
        landed = reruns.get(
            (
                void.agent_id,
                void.bench_version,
                void.validator_hotkey,
                void.seed,
                void.void_generation,
            )
        )
        items.append(
            InfraVoidRecord(
                void_id=void.void_id,
                agent_id=void.agent_id,
                bench_version=void.bench_version,
                validator_hotkey=void.validator_hotkey,
                seed=str(void.seed),
                void_generation=void.void_generation,
                sample_kind=void.sample_kind,
                run_id=void.run_id,
                composite=void.composite,
                reason=void.reason,
                evidence=void.evidence,
                evidence_digest=void.evidence_digest,
                actor=void.actor,
                source=void.source,  # type: ignore[arg-type]
                outage_started_at=void.outage_started_at,
                outage_ended_at=void.outage_ended_at,
                created_at=void.created_at,
                rerun_composite=landed[0] if landed else None,
                rerun_run_id=landed[1] if landed else None,
            )
        )
    return InfraVoidList(count=len(items), limit=limit, offset=offset, items=items)

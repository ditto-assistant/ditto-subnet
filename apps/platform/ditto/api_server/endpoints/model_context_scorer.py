"""Trusted CPM scorer endpoints for the model_context competition.

The scorer (``services/cpm-scorer``, contract section 6.1) authenticates with
``DITTO_MODEL_CONTEXT_SCORER_TOKEN``; unset disables every endpoint (503). The
token is compared in constant time and never logged.

``GET /work`` leases the next run of the open season (reference runs of the
unmodified bases first) or answers 204. ``POST /results`` and ``POST /fault``
accept a post only for the run's current, unexpired lease. ``GET
/reference-work`` reports the season's base floors and which are still
missing.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.model_context import (
    ModelContextFaultAck,
    ModelContextFaultRequest,
    ModelContextReferenceWork,
    ModelContextResultRequest,
    ModelContextResultResponse,
    ModelContextWorkItem,
    ModelContextWorkSettings,
    ModelContextWorkSubmission,
    generate_seed,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.model_context_competition import (
    ResultRejected,
    current_season,
    lease_next_run,
    load_settings,
    record_fault,
    record_heartbeat,
    record_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/model-context-scorer", tags=["model_context"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_model_context_scorer(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.config.model_context_scorer_token
    if expected is None:
        raise HTTPException(
            status_code=503, detail="model_context scorer is not configured"
        )
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing scorer bearer token")
    if not secrets.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="invalid scorer bearer token")


ScorerDep = Annotated[None, Depends(require_model_context_scorer)]
_SCORER_ID = r"^[A-Za-z0-9._:-]+$"


@router.get(
    "/work",
    response_model=ModelContextWorkItem,
    responses={204: {"description": "Nothing to score."}},
)
async def lease_work(
    _scorer: ScorerDep,
    session: SessionDep,
    scorer_id: Annotated[str, Query(min_length=1, max_length=120, pattern=_SCORER_ID)],
    scorer_version: Annotated[str | None, Query(max_length=64)] = None,
    gpu_sku: Annotated[str | None, Query(max_length=64)] = None,
) -> ModelContextWorkItem | Response:
    """Lease the next run of the open season, or 204."""
    now = datetime.now(UTC)
    await record_heartbeat(
        session,
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        gpu_sku=gpu_sku,
        poll="work",
        now=now,
    )
    resolved = await load_settings(session)
    lease = await lease_next_run(
        session, resolved=resolved, scorer_id=scorer_id, now=now
    )
    if lease is None:
        await session.commit()
        return Response(status_code=204)
    settings = resolved.settings
    run, season, row = lease.run, lease.season, lease.submission
    # lease_next_run never leases a run whose base left the allowlist.
    base = settings.base(run.base_id)
    if base is None:
        raise RuntimeError(f"leased run {run.run_id} has no allowlisted base")
    assert run.lease_id is not None and run.lease_expires_at is not None
    work = ModelContextWorkItem(
        run_id=run.run_id,
        lease_id=run.lease_id,
        lease_expires_at=run.lease_expires_at,
        kind=run.kind,  # type: ignore[arg-type]
        submission=(
            ModelContextWorkSubmission(
                submission_id=row.submission_id,
                hotkey=row.hotkey,
                hf_repo=row.hf_repo,
                revision=row.revision,
                manifest=dict(row.manifest),
                manifest_sha256=row.manifest_sha256,
                weights_sha256=row.weights_sha256,
                base_id=row.base_id,
                kind=row.kind,  # type: ignore[arg-type]
            )
            if row is not None
            else None
        ),
        base=base,
        season=season.season,
        seeds=[int(s) for s in season.seeds],
        generate_seeds=[generate_seed(int(s)) for s in season.seeds],
        bench_version=season.bench_version,
        cpm_score_version=run.cpm_score_version,
        gpu_sku=season.gpu_sku,
        settings=ModelContextWorkSettings(
            budget_tokens=settings.budget_tokens,
            tool_k=settings.tool_k,
            floor_margin=settings.floor_margin,
        ),
    )
    await session.commit()
    logger.info(
        "model_context run leased run_id=%s kind=%s season=%d attempt=%d scorer=%s",
        run.run_id,
        run.kind,
        season.season,
        run.attempts,
        scorer_id,
    )
    return work


@router.post("/results", response_model=ModelContextResultResponse)
async def post_results(
    payload: ModelContextResultRequest, _scorer: ScorerDep, session: SessionDep
) -> ModelContextResultResponse:
    """Record one scored run for its current lease."""
    try:
        recorded = await record_result(session, payload, now=datetime.now(UTC))
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.info(
        "model_context run scored run_id=%s kind=%s season=%d score=%.6f",
        recorded.run.run_id,
        recorded.run.kind,
        recorded.run.season,
        payload.score,
    )
    return ModelContextResultResponse(
        run_id=recorded.run.run_id,
        status="scored",
        score=payload.score,
        submission_status=recorded.submission_status,  # type: ignore[arg-type]
        base_floor=recorded.base_floor,
    )


@router.post("/fault", response_model=ModelContextFaultAck)
async def post_fault(
    payload: ModelContextFaultRequest, _scorer: ScorerDep, session: SessionDep
) -> ModelContextFaultAck:
    """Record a scorer fault for the run's current lease."""
    resolved = await load_settings(session)
    try:
        recorded = await record_fault(
            session, payload, settings=resolved.settings, now=datetime.now(UTC)
        )
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.warning(
        "model_context run fault run_id=%s kind=%s action=%s scorer=%s",
        payload.run_id,
        payload.error_kind,
        recorded.action,
        payload.scorer_id,
    )
    return ModelContextFaultAck(
        run_id=recorded.run.run_id,
        action=recorded.action,  # type: ignore[arg-type]
        run_status=recorded.run.status,  # type: ignore[arg-type]
        submission_status=recorded.submission_status,  # type: ignore[arg-type]
    )


@router.get("/reference-work", response_model=ModelContextReferenceWork)
async def reference_work(
    _scorer: ScorerDep, session: SessionDep
) -> ModelContextReferenceWork:
    """The open season's base floors, and the bases still without one."""
    settings = (await load_settings(session)).settings
    season = await current_season(session)
    floors = (
        {k: float(v) for k, v in (season.base_floors or {}).items()}
        if season is not None
        else {}
    )
    return ModelContextReferenceWork(
        season=season.season if season is not None else None,
        bench_version=season.bench_version if season is not None else None,
        seeds=[int(s) for s in season.seeds] if season is not None else [],
        floor_margin=settings.floor_margin,
        base_floors=floors,
        bases=list(settings.base_allowlist),
        pending=[
            base.base_id
            for base in settings.base_allowlist
            if season is not None and base.base_id not in floors
        ],
    )

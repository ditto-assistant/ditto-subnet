"""Trusted generator-competition scorer: lease runs, post evaluations.

The scorer (``services/dittobench-api/cmd/challenge-scorer``) authenticates
with ``DITTO_GENERATOR_SCORER_TOKEN``; unset disables both endpoints. The token
is compared in constant time and never logged.

``GET /generator-scorer/work`` row-locks the most overdue active generator,
creates a run on fresh secret seeds with the current probe seed and a panel
snapshot, and serves the exact uploaded source archive. ``POST
/generator-scorer/results`` accepts an evaluation only for the current lease
and scores it against that run, so a scorer cannot choose the seeds, the
panel, an agent's owner, or the champion.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    GeneratorPanelSlot,
    GeneratorResultRequest,
    GeneratorResultResponse,
    GeneratorWorkItem,
    GeneratorWorkResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.generator_competition import (
    ResultRejected,
    lease_next,
    load_settings,
    record_result,
)
from ditto.db.models import GeneratorProbeEpoch
from ditto.db.queries.generator import get_run, get_submission
from ditto.generator_scoring import ScoreInputError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generator-scorer", tags=["generator"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_generator_scorer(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.config.generator_scorer_token
    if expected is None:
        raise HTTPException(
            status_code=503, detail="generator scorer is not configured"
        )
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing scorer bearer token")
    if not secrets.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="invalid scorer bearer token")


ScorerDep = Annotated[None, Depends(require_generator_scorer)]


@router.get("/work", response_model=GeneratorWorkResponse)
async def lease_work(_scorer: ScorerDep, session: SessionDep) -> GeneratorWorkResponse:
    """Lease the most overdue generator run, or return ``{"work": null}``."""
    resolved = await load_settings(session)
    if resolved.settings.mode == "off":
        return GeneratorWorkResponse()
    lease = await lease_next(session, resolved=resolved, now=datetime.now(UTC))
    if lease is None:
        # A backoff may have been written while scanning; keep it.
        await session.commit()
        return GeneratorWorkResponse()
    row, run = lease.submission, lease.run
    work = GeneratorWorkItem(
        lease_id=row.lease_id,  # type: ignore[arg-type]
        run_id=run.run_id,
        submission_id=row.submission_id,
        miner_hotkey=row.miner_hotkey,
        source_sha256=row.source_sha256,
        source_base64=base64.b64encode(row.source_bytes).decode("ascii"),
        seeds=list(run.seeds),
        probe_seed=lease.probe_seed,
        cases_per_seed=run.cases_per_seed,
        panel=[GeneratorPanelSlot(**slot) for slot in run.panel],
        lease_expires_at=row.lease_expires_at,  # type: ignore[arg-type]
    )
    await session.commit()
    logger.info(
        "generator run leased submission_id=%s run_id=%s attempt=%d panel=%d",
        row.submission_id,
        run.run_id,
        run.attempt,
        len(run.panel),
    )
    return GeneratorWorkResponse(work=work)


@router.post("/results", response_model=GeneratorResultResponse)
async def post_results(
    payload: GeneratorResultRequest, _scorer: ScorerDep, session: SessionDep
) -> GeneratorResultResponse:
    """Record one scorer evaluation for the current lease."""
    row = await get_submission(session, payload.submission_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    run = await get_run(session, payload.run_id, for_update=True)
    if run is None or run.submission_id != row.submission_id:
        raise HTTPException(status_code=404, detail="unknown run")
    probe = await session.get(GeneratorProbeEpoch, run.probe_epoch)
    if probe is None:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="the run's probe epoch no longer exists"
        )
    try:
        recorded = await record_result(
            session,
            row,
            run,
            payload,
            probe_seed=probe.seed,
            now=datetime.now(UTC),
        )
    except ResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScoreInputError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    logger.info(
        "generator run result submission_id=%s run_id=%s status=%s outcome=%s score=%s",
        payload.submission_id,
        payload.run_id,
        recorded.status,
        recorded.outcome,
        recorded.score,
    )
    return GeneratorResultResponse(
        run_id=payload.run_id,
        status=recorded.status,  # type: ignore[arg-type]
        outcome=recorded.outcome,
        score=recorded.score,
        submission_status=recorded.submission_status,  # type: ignore[arg-type]
    )

"""Trusted generator-competition scorer: qualification runs and round work.

The scorer (``services/dittobench-api/cmd/challenge-scorer``) authenticates
with ``DITTO_GENERATOR_SCORER_TOKEN``; unset disables every endpoint. The
token is compared in constant time and never logged.

Qualification: ``GET /generator-scorer/work`` row-locks the most overdue
unqualified generator, creates a run on fresh secret seeds with the current
probe seed, and serves the exact uploaded source archive. ``POST
/generator-scorer/results`` accepts an evaluation only for the current lease.

Faults: ``POST /generator-scorer/fault`` reports a leased item the scorer could
not do through no fault of the generator: a request its pack contract refuses
(``unsupported_request``, e.g. a stale scorer below the leased case count) or a
sandbox failure. A qualification backs off and stays active; a round is voided
with an infrastructure reason or re-leased. A fault never rejects a generator.

Rounds: ``GET /generator-scorer/round-work`` leases a round that needs its
dataset generated (``kind=generate``: frozen columns, their sources, the round
seed) or its matrix scored (``kind=score``: the columns' case refs, every
panel agent's validator results, claimed fingerprints, and the score
parameters). ``POST /round-results`` and ``POST /round-score`` accept work only
for the round's current lease, so the scorer never chooses a seed, a column,
a panel agent, an owner, or the champion.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    GeneratorResultRequest,
    GeneratorResultResponse,
    GeneratorScorerFault,
    GeneratorScorerFaultAck,
    GeneratorWorkItem,
    GeneratorWorkResponse,
)
from ditto.api_models.generator_rounds import (
    GeneratorRoundAck,
    GeneratorRoundGenerateResult,
    GeneratorRoundScoreResult,
    GeneratorRoundWorkResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.generator_competition import (
    ResultRejected,
    lease_next,
    load_settings,
    record_result,
    record_scorer_fault,
)
from ditto.api_server.generator_rounds import (
    RoundResultRejected,
    lease_round_work,
    record_generation,
    record_round_fault,
    record_round_score,
)
from ditto.api_server.generator_scorer_capability import (
    Poll,
    ScorerCapability,
    record_heartbeat,
    refusal,
)
from ditto.db.models import GeneratorProbeEpoch
from ditto.db.queries.generator import get_run, get_submission

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


def advertised_capability(
    scorer_id: Annotated[
        str | None, Query(max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None,
    scorer_version: Annotated[str | None, Query(max_length=64)] = None,
    min_cases: Annotated[int | None, Query(ge=1, le=100_000)] = None,
    max_cases: Annotated[int | None, Query(ge=1, le=100_000)] = None,
    sandbox: Annotated[str | None, Query(max_length=32)] = None,
    build_revision: Annotated[
        str | None, Query(max_length=64, pattern=r"^[0-9a-f]{7,64}$")
    ] = None,
) -> ScorerCapability | None:
    """The capability a poll advertised, or ``None`` for a scorer predating it."""
    if not scorer_version:
        return None
    if (min_cases is None) != (max_cases is None) or (
        min_cases is not None and max_cases is not None and max_cases < min_cases
    ):
        raise HTTPException(
            status_code=422, detail="min_cases and max_cases go together, min <= max"
        )
    return ScorerCapability(
        scorer_id=scorer_id or "default",
        scorer_version=scorer_version,
        min_cases=min_cases,
        max_cases=max_cases,
        sandbox=sandbox or None,
        build_revision=build_revision,
    )


CapabilityDep = Annotated[ScorerCapability | None, Depends(advertised_capability)]


async def _heartbeat(
    session: AsyncSession,
    capability: ScorerCapability | None,
    *,
    poll: Poll,
    now: datetime,
) -> None:
    if capability is not None:
        await record_heartbeat(session, capability, poll=poll, now=now)


@router.get("/work", response_model=GeneratorWorkResponse)
async def lease_work(
    _scorer: ScorerDep, session: SessionDep, capability: CapabilityDep
) -> GeneratorWorkResponse:
    """Lease the most overdue qualification run, or return ``{"work": null}``.

    Nothing is leased while ``cases_per_seed`` is outside the polling scorer's
    advertised bounds: a stale scorer would only fail it.
    """
    now = datetime.now(UTC)
    await _heartbeat(session, capability, poll="qualification", now=now)
    resolved = await load_settings(session)
    if resolved.settings.mode == "off":
        await session.commit()
        return GeneratorWorkResponse()
    refused = refusal(capability, resolved.settings.cases_per_seed)
    if refused is not None:
        await session.commit()
        logger.warning("generator qualification not leased: %s", refused)
        return GeneratorWorkResponse()
    lease = await lease_next(session, resolved=resolved, now=now)
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
        lease_expires_at=row.lease_expires_at,  # type: ignore[arg-type]
    )
    await session.commit()
    logger.info(
        "generator qualification leased submission_id=%s run_id=%s attempt=%d",
        row.submission_id,
        run.run_id,
        run.attempt,
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


@router.post("/fault", response_model=GeneratorScorerFaultAck)
async def post_fault(
    payload: GeneratorScorerFault, _scorer: ScorerDep, session: SessionDep
) -> GeneratorScorerFaultAck:
    """Record a scorer-side fault for the current lease; never blames a generator."""
    now = datetime.now(UTC)
    if payload.work == "round":
        try:
            row = await record_round_fault(session, payload, now=now)
        except RoundResultRejected as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
        logger.warning(
            "generator scorer fault round=%s kind=%s status=%s scorer=%s",
            row.round_index,
            payload.error_kind,
            row.status,
            payload.scorer_version,
        )
        return GeneratorScorerFaultAck(
            work="round",
            action="voided" if row.status == "void" else "released",
            round_status=row.status,
        )
    if payload.submission_id is None or payload.run_id is None:
        raise HTTPException(
            status_code=422, detail="a qualification fault names its run"
        )
    sub = await get_submission(session, payload.submission_id, for_update=True)
    if sub is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    run = await get_run(session, payload.run_id, for_update=True)
    if run is None or run.submission_id != sub.submission_id:
        raise HTTPException(status_code=404, detail="unknown run")
    try:
        recorded = await record_scorer_fault(session, sub, run, payload, now=now)
    except ResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.warning(
        "generator scorer fault submission_id=%s run_id=%s kind=%s scorer=%s",
        payload.submission_id,
        payload.run_id,
        payload.error_kind,
        payload.scorer_version,
    )
    return GeneratorScorerFaultAck(
        work="qualification",
        action="backed_off",
        submission_status=recorded.submission_status,  # type: ignore[arg-type]
    )


@router.get("/round-work", response_model=GeneratorRoundWorkResponse)
async def lease_round(
    _scorer: ScorerDep, session: SessionDep, capability: CapabilityDep
) -> GeneratorRoundWorkResponse:
    """Lease the oldest round needing generation or scoring, if any.

    A generating round whose ``cases_per_column`` the polling scorer refuses
    is voided with an ``infra:`` reason instead of leased.
    """
    now = datetime.now(UTC)
    await _heartbeat(session, capability, poll="round", now=now)
    resolved = await load_settings(session)
    try:
        lease = await lease_round_work(
            session, resolved=resolved, now=now, capability=capability
        )
    except RoundResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    if lease is None:
        return GeneratorRoundWorkResponse()
    logger.info(
        "generator round leased round=%s kind=%s attempt=%d",
        lease.row.round_index,
        lease.work.kind,
        lease.row.lease_attempts,
    )
    return GeneratorRoundWorkResponse(work=lease.work)


@router.post("/round-results", response_model=GeneratorRoundAck)
async def post_round_results(
    payload: GeneratorRoundGenerateResult, _scorer: ScorerDep, session: SessionDep
) -> GeneratorRoundAck:
    """Store a round's generated dataset for the current generation lease."""
    try:
        row = await record_generation(session, payload, now=datetime.now(UTC))
    except RoundResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.info(
        "generator round generated round=%s status=%s dataset=%s",
        row.round_index,
        row.status,
        row.dataset_sha256,
    )
    return GeneratorRoundAck(round_index=row.round_index, status=row.status)  # type: ignore[arg-type]


@router.post("/round-score", response_model=GeneratorRoundAck)
async def post_round_score(
    payload: GeneratorRoundScoreResult, _scorer: ScorerDep, session: SessionDep
) -> GeneratorRoundAck:
    """Store a round's scored matrix for the current scoring lease."""
    try:
        row = await record_round_score(session, payload, now=datetime.now(UTC))
    except RoundResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.info("generator round scored round=%s", row.round_index)
    return GeneratorRoundAck(round_index=row.round_index, status=row.status)  # type: ignore[arg-type]

"""Trusted generator-competition scorer: lease packs, post panel results.

The scorer (``services/dittobench-api/cmd/challenge-scorer``) authenticates
with ``DITTO_GENERATOR_SCORER_TOKEN``; unset disables both endpoints. The token
is compared in constant time and never logged.

``GET /generator-scorer/work`` row-locks the oldest leasable pack, snapshots
the current panel onto it, and serves the exact uploaded bytes. ``POST
/generator-scorer/results`` accepts a result only for the current lease and
scores it against that snapshot, so a scorer cannot choose the panel, an
agent's owner, or the champion.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime, timedelta
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
from ditto.api_server import generator_competition
from ditto.api_server.dependencies import get_session
from ditto.api_server.generator_competition import (
    ResultRejected,
    load_settings,
    record_result,
)
from ditto.db.queries.generator import (
    finish_pack,
    get_pack,
    lease_pack,
    lock_next_leasable_pack,
)
from ditto.generator_scoring import ScoreInputError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generator-scorer", tags=["generator"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Packs whose leases keep expiring are retired rather than re-leased forever.
_MAX_LEASE_SCAN = 8


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
    """Lease the oldest ready pack, or return ``{"work": null}``."""
    resolved = await load_settings(session)
    settings = resolved.settings
    if settings.mode == "off":
        return GeneratorWorkResponse()
    now = datetime.now(UTC)
    for _ in range(_MAX_LEASE_SCAN):
        row = await lock_next_leasable_pack(session, now=now)
        if row is None:
            await session.rollback()
            return GeneratorWorkResponse()
        if row.attempts >= settings.max_attempts:
            finish_pack(row, status="failed", outcome="lease_attempts_exhausted")
            await session.commit()
            continue
        panel = await generator_competition.select_panel(
            session, size=settings.panel_size
        )
        lease_pack(
            row, now=now, ttl=timedelta(seconds=settings.lease_ttl_seconds), panel=panel
        )
        work = GeneratorWorkItem(
            lease_id=row.lease_id,  # type: ignore[arg-type]
            pack_id=row.pack_id,
            pack_sha256=row.pack_sha256,
            pack_base64=base64.b64encode(row.pack_bytes).decode("ascii"),
            panel=[GeneratorPanelSlot(**slot) for slot in panel],
            lease_expires_at=row.lease_expires_at,  # type: ignore[arg-type]
        )
        await session.commit()
        logger.info(
            "generator pack leased pack_id=%s attempt=%d panel=%d",
            work.pack_id,
            row.attempts,
            len(panel),
        )
        return GeneratorWorkResponse(work=work)
    await session.rollback()
    return GeneratorWorkResponse()


@router.post("/results", response_model=GeneratorResultResponse)
async def post_results(
    payload: GeneratorResultRequest, _scorer: ScorerDep, session: SessionDep
) -> GeneratorResultResponse:
    """Record one scorer result for the current lease."""
    resolved = await load_settings(session)
    row = await get_pack(session, payload.pack_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown pack")
    try:
        recorded = await record_result(
            session, row, payload, resolved=resolved, now=datetime.now(UTC)
        )
    except ResultRejected as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScoreInputError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    logger.info(
        "generator pack result pack_id=%s status=%s outcome=%s score=%s",
        payload.pack_id,
        recorded.status,
        recorded.outcome,
        recorded.score,
    )
    return GeneratorResultResponse(
        pack_id=payload.pack_id,
        status=recorded.status,  # type: ignore[arg-type]
        outcome=recorded.outcome,
        score=recorded.score,
    )

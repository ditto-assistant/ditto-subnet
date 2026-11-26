"""model_context (CPM) competition: miner submissions and the public board.

``POST /model-context/submissions`` admits one signed submission. Checks run
cheap-before-expensive and nothing is written until all pass: lane open, a
fresh signature over the contract section 3.2 payload, ban, fee availability,
subnet registration, manifest schema and base allowlist, cooldown, the
on-chain ``dcpm1:<manifest_sha256>`` commitment (whose block is the
submission's priority), and no standing copy of the same manifest from this
hotkey. The submission is then stored as ``screening``; the Platform loop
downloads and screens the pinned revision (:mod:`ditto.model_context_artifacts`)
and queues it, so the request never waits on a multi-GB download.

Weights are public by contract (section 2), so the board and listings name the
repo and revision of every submission.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.model_context import (
    MODEL_CONTEXT_COMPETITION,
    ModelContextBoardResponse,
    ModelContextSeasonListResponse,
    ModelContextSettings,
    ModelContextSubmissionListResponse,
    ModelContextSubmissionRequest,
    ModelContextSubmissionResponse,
    ModelContextUploadTerms,
    commitment_value,
    signature_payload,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.model_context_competition import (
    build_board,
    cooldown_until,
    current_season,
    insert_submission,
    latest_runs,
    latest_submitted_at,
    list_seasons,
    load_settings,
    lock_hotkey,
    same_manifest,
    season_view,
    submission_summary,
)
from ditto.api_server.upload_feedback import hotkey_not_registered_message
from ditto.chain.errors import ChainError
from ditto.db.models import ModelContextSubmission
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.submission_settings import effective_submission_settings
from ditto.model_context_artifacts import ArtifactRejected, parse_manifest

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/model-context", tags=["model_context"])

SIGNATURE_MAX_SKEW_SECONDS = 300
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_PUBLIC_READ_CACHE = "public, max-age=30, stale-while-revalidate=120"
CLOSED_DETAIL = "the model_context competition is not accepting submissions"
FEE_UNAVAILABLE_DETAIL = (
    "model_context uploads are priced with a fee this Platform cannot collect "
    "yet; no submission is accepted until the fee path ships"
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated["ChainClient", Depends(get_chain_client)]


def verify_signature(payload: ModelContextSubmissionRequest, *, now: float) -> bool:
    if abs(now - payload.ts) > SIGNATURE_MAX_SKEW_SECONDS:
        return False
    message = signature_payload(
        hotkey=payload.hotkey,
        hf_repo=payload.hf_repo,
        revision=payload.revision,
        manifest_sha256=payload.manifest_sha256,
        ts=payload.ts,
        nonce=payload.nonce,
    )
    try:
        keypair = bittensor.Keypair(ss58_address=payload.hotkey)
        return bool(
            keypair.verify(message, bytes.fromhex(payload.signature.removeprefix("0x")))
        )
    except (ValueError, TypeError):
        return False


async def _fee_required(
    session: AsyncSession, request: Request, settings: ModelContextSettings
) -> bool:
    effective = await effective_submission_settings(
        session,
        default_payment_address=request.app.state.config.upload_payment_address,
    )
    return effective.competition_fee(
        MODEL_CONTEXT_COMPETITION,
        competition_requires_fee=settings.upload_fee_required,
    ).fee_required


@router.get("/upload-terms", response_model=ModelContextUploadTerms)
async def upload_terms(
    request: Request,
    session: SessionDep,
    response: Response,
    hotkey: Annotated[str | None, Query(pattern=_SS58)] = None,
) -> ModelContextUploadTerms:
    """What a submission needs now; read it before writing the commitment."""
    response.headers["Cache-Control"] = "no-store"
    settings = (await load_settings(session)).settings
    accepting = settings.mode != "off"
    blockers: list[str] = []
    if not accepting:
        blockers.append(CLOSED_DETAIL)
    fee_required = await _fee_required(session, request, settings)
    if accepting and fee_required:
        blockers.append(FEE_UNAVAILABLE_DETAIL)
    retry_at = None
    if hotkey is not None:
        if await is_hotkey_banned(session, hotkey=hotkey):
            blockers.append("hotkey is banned from submitting")
        retry_at = cooldown_until(
            await latest_submitted_at(session, hotkey), settings, datetime.now(UTC)
        )
        if retry_at is not None:
            blockers.append(
                f"model_context submission cooldown until {retry_at.isoformat()}"
            )
    return ModelContextUploadTerms(
        accepting=accepting,
        mode=settings.mode,
        payment_required=fee_required,
        upload_cooldown_seconds=settings.upload_cooldown_seconds,
        retry_at=retry_at,
        blockers=blockers,
        bases=list(settings.base_allowlist),
        max_lora_rank=settings.max_lora_rank,
        max_total_bytes=settings.max_total_bytes,
    )


@router.post("/submissions", response_model=ModelContextSubmissionResponse)
async def submit(
    payload: ModelContextSubmissionRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> ModelContextSubmissionResponse:
    """Admit one signed, committed submission for screening."""
    settings = (await load_settings(session)).settings
    if settings.mode == "off":
        raise HTTPException(status_code=403, detail=CLOSED_DETAIL)
    if not verify_signature(payload, now=time.time()):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )
    if await is_hotkey_banned(session, hotkey=payload.hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")
    if await _fee_required(session, request, settings):
        raise HTTPException(status_code=503, detail=FEE_UNAVAILABLE_DETAIL)
    try:
        manifest = parse_manifest(payload.manifest, settings)
    except ArtifactRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    retry_at = cooldown_until(
        await latest_submitted_at(session, payload.hotkey),
        settings,
        datetime.now(UTC),
    )
    _refuse_cooldown(response, retry_at)
    if session.in_transaction():
        await session.rollback()

    netuid = request.app.state.config.chain.netuid
    try:
        registration = await chain.get_hotkey_registration(payload.hotkey, netuid)
    except ChainError as exc:
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    if not registration.registered:
        raise HTTPException(
            status_code=400, detail=hotkey_not_registered_message(registration)
        )
    manifest_sha256 = payload.manifest_sha256
    expected = commitment_value(manifest_sha256)
    try:
        commitment = await chain.get_hotkey_commitment(payload.hotkey, netuid)
    except ChainError as exc:
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    if commitment is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"hotkey has no commitment on netuid {netuid}; set {expected} "
                "with Commitments.set_commitment first"
            ),
        )
    if commitment.text != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"the hotkey's on-chain commitment (block {commitment.block}) is "
                f"not {expected}; commit this manifest's digest first"
            ),
        )

    now = datetime.now(UTC)
    await lock_hotkey(session, payload.hotkey)
    _refuse_cooldown(
        response,
        cooldown_until(
            await latest_submitted_at(session, payload.hotkey), settings, now
        ),
    )
    existing = await same_manifest(
        session, hotkey=payload.hotkey, manifest_sha256=manifest_sha256
    )
    if existing is not None:
        existing_id = existing.submission_id
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"this manifest was already submitted as {existing_id}",
        )
    row = await insert_submission(
        session,
        hotkey=payload.hotkey,
        hf_repo=payload.hf_repo,
        revision=payload.revision,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        commitment_block=commitment.block,
        now=now,
    )
    await session.commit()
    logger.info(
        "model_context submission admitted hotkey=%s submission_id=%s base=%s "
        "kind=%s commitment_block=%d",
        payload.hotkey,
        row.submission_id,
        row.base_id,
        row.kind,
        row.commitment_block,
    )
    return ModelContextSubmissionResponse(
        submission_id=row.submission_id,
        status="screening",
        manifest_sha256=manifest_sha256,
        commitment_block=row.commitment_block,
        base_id=row.base_id,
        kind=manifest.kind,
    )


def _refuse_cooldown(response: Response, retry_at: datetime | None) -> None:
    if retry_at is None:
        return
    retry_after = str(int((retry_at - datetime.now(UTC)).total_seconds()) + 1)
    response.headers["Retry-After"] = retry_after
    raise HTTPException(
        status_code=429,
        detail=f"model_context submission cooldown until {retry_at.isoformat()}",
        headers={"Retry-After": retry_after},
    )


@router.get("/submissions", response_model=ModelContextSubmissionListResponse)
async def list_submissions(
    session: SessionDep,
    response: Response,
    hotkey: Annotated[str, Query(pattern=_SS58)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ModelContextSubmissionListResponse:
    """A hotkey's submissions, newest first, with each one's latest run."""
    response.headers["Cache-Control"] = "no-store"
    rows = list(
        await session.scalars(
            select(ModelContextSubmission)
            .where(ModelContextSubmission.hotkey == hotkey)
            .order_by(ModelContextSubmission.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    latest = await latest_runs(session, [row.submission_id for row in rows])
    return ModelContextSubmissionListResponse(
        submissions=[
            submission_summary(row, latest.get(row.submission_id)) for row in rows
        ]
    )


@router.get(
    "/submissions/{submission_id}",
    response_model=ModelContextSubmissionListResponse,
)
async def get_submission(
    submission_id: UUID, session: SessionDep, response: Response
) -> ModelContextSubmissionListResponse:
    """One submission and its latest run."""
    response.headers["Cache-Control"] = "no-store"
    row = await session.get(ModelContextSubmission, submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown model_context submission")
    latest = await latest_runs(session, [row.submission_id])
    return ModelContextSubmissionListResponse(
        submissions=[submission_summary(row, latest.get(row.submission_id))]
    )


@router.get("/board", response_model=ModelContextBoardResponse)
async def board(session: SessionDep, response: Response) -> ModelContextBoardResponse:
    """The current season, ranked by score then commitment block."""
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    resolved = await load_settings(session)
    return await build_board(session, resolved=resolved, now=datetime.now(UTC))


@router.get("/seasons", response_model=ModelContextSeasonListResponse)
async def seasons(
    session: SessionDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ModelContextSeasonListResponse:
    """Recent seasons: open block, its hash, the seeds derived from it, and
    each base's floor."""
    response.headers["Cache-Control"] = _PUBLIC_READ_CACHE
    current = await current_season(session)
    return ModelContextSeasonListResponse(
        current=current.season if current is not None else None,
        seasons=[
            season_view(row)
            for row in await list_seasons(session, limit=limit, offset=offset)
        ],
    )

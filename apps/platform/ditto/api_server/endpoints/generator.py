"""Generator competition: miner uploads and the public board (shadow).

``POST /generator/submissions`` accepts a signed generator source archive. The
checks run cheap-before-expensive and every mutation happens after every check
passes: lane open, fresh domain-separated signature, ban, subnet registration,
archive bytes and structure (the Go contract's rules), duplicate source,
duplicate tree, cooldown, then one insert that supersedes the hotkey's
previous generator.

Source never leaves Platform except to the trusted scorer. The listing and
board here expose identities, lifecycle, and aggregate outcomes only.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    GeneratorSubmissionListResponse,
    GeneratorSubmissionUploadRequest,
    GeneratorSubmissionUploadResponse,
    source_signature_payload,
)
from ditto.api_models.generator_ledger import GeneratorLedgerResponse
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.generator_competition import (
    UploadRejected,
    build_ledger,
    inspect_upload,
    load_settings,
    submission_summary,
)
from ditto.chain.errors import ChainError
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.generator import (
    case_status_counts,
    get_submission_by_sha,
    get_submission_by_tree,
    insert_submission,
    latest_runs,
    latest_submitted_at,
    list_submissions,
    lock_hotkey_uploads,
    lock_source_tree,
    supersede_active,
)
from ditto.db.queries.scores import emission_owner

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generator", tags=["generator"])

SIGNATURE_MAX_SKEW_SECONDS = 300
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated["ChainClient", Depends(get_chain_client)]


def _verify_signature(payload: GeneratorSubmissionUploadRequest) -> bool:
    if (
        payload.signature_nonce.version != 4
        or abs(time.time() - payload.signature_timestamp) > SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    message = source_signature_payload(
        hotkey=payload.hotkey,
        source_sha256=payload.source_sha256,
        timestamp=payload.signature_timestamp,
        nonce=payload.signature_nonce,
    )
    signature = payload.signature.removeprefix("0x")
    try:
        keypair = bittensor.Keypair(ss58_address=payload.hotkey)
        return bool(keypair.verify(message, bytes.fromhex(signature)))
    except (ValueError, TypeError):
        return False


@router.post("/submissions", response_model=GeneratorSubmissionUploadResponse)
async def upload_submission(
    payload: GeneratorSubmissionUploadRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> GeneratorSubmissionUploadResponse:
    """Accept one signed generator source archive and make it the hotkey's
    active generator."""
    resolved = await load_settings(session)
    settings = resolved.settings
    if settings.mode == "off":
        raise HTTPException(
            status_code=403,
            detail="the generator competition is not accepting submissions",
        )
    if not _verify_signature(payload):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )
    if await is_hotkey_banned(session, hotkey=payload.hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")
    if session.in_transaction():
        await session.rollback()

    netuid = request.app.state.config.chain.netuid
    try:
        neurons = await chain.get_recent_neurons(netuid)
    except ChainError as exc:
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    neuron = next((n for n in neurons if n.hotkey == payload.hotkey), None)
    if neuron is None:
        raise HTTPException(
            status_code=400, detail=f"hotkey not registered on netuid {netuid}"
        )

    try:
        upload = inspect_upload(
            source_base64=payload.source_base64, source_sha256=payload.source_sha256
        )
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await lock_hotkey_uploads(session, payload.hotkey)
    await lock_source_tree(session, upload.info.tree_sha256)
    existing = await get_submission_by_sha(session, payload.source_sha256)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this exact archive was already uploaded as {existing.submission_id}"
            ),
        )
    same_tree = await get_submission_by_tree(session, upload.info.tree_sha256)
    if same_tree is not None:
        # Re-packing identical files is not a new generator, whoever uploads it.
        raise HTTPException(
            status_code=409,
            detail=(
                "these exact source files were already uploaded as "
                f"{same_tree.submission_id}"
            ),
        )
    now = datetime.now(UTC)
    last = await latest_submitted_at(session, miner_hotkey=payload.hotkey)
    if last is not None:
        retry_at = last + timedelta(seconds=settings.upload_cooldown_seconds)
        if retry_at > now:
            retry_after = str(int((retry_at - now).total_seconds()) + 1)
            response.headers["Retry-After"] = retry_after
            raise HTTPException(
                status_code=429,
                detail=f"generator upload cooldown until {retry_at.isoformat()}",
                headers={"Retry-After": retry_after},
            )

    try:
        row = await insert_submission(
            session,
            miner_hotkey=payload.hotkey,
            owner_key=emission_owner(
                miner_hotkey=payload.hotkey, miner_coldkey=neuron.coldkey or None
            ),
            source_sha256=payload.source_sha256,
            tree_sha256=upload.info.tree_sha256,
            source_bytes=upload.raw,
            module=upload.info.module,
            go_version=upload.info.go_version,
            file_count=upload.info.files,
            now=now,
        )
        superseded = await supersede_active(
            session, miner_hotkey=payload.hotkey, keep=row.submission_id, now=now
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="this exact archive was uploaded concurrently"
        ) from exc
    logger.info(
        "generator submission accepted hotkey=%s submission_id=%s files=%d "
        "superseded=%d",
        payload.hotkey,
        row.submission_id,
        row.file_count,
        len(superseded),
    )
    return GeneratorSubmissionUploadResponse(
        submission_id=row.submission_id,
        source_sha256=row.source_sha256,
        tree_sha256=row.tree_sha256,
        status="active",
        superseded=superseded,
    )


@router.get("/submissions", response_model=GeneratorSubmissionListResponse)
async def list_hotkey_submissions(
    session: SessionDep,
    hotkey: Annotated[str, Query(pattern=_SS58)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> GeneratorSubmissionListResponse:
    """A hotkey's recent generators: lifecycle and aggregate outcome only."""
    rows = await list_submissions(session, miner_hotkey=hotkey, limit=limit)
    latest = await latest_runs(session, [row.submission_id for row in rows])
    counts = await case_status_counts(session, [run.run_id for run in latest.values()])
    return GeneratorSubmissionListResponse(
        submissions=[
            submission_summary(
                row,
                latest.get(row.submission_id),
                counts.get(latest[row.submission_id].run_id)
                if row.submission_id in latest
                else None,
            )
            for row in rows
        ]
    )


@router.get("/board", response_model=GeneratorLedgerResponse)
async def board(session: SessionDep, response: Response) -> GeneratorLedgerResponse:
    """The shadow generator ledger (each hotkey's latest scored run)."""
    response.headers["Cache-Control"] = "public, max-age=30"
    resolved = await load_settings(session)
    return await build_ledger(session, resolved=resolved, now=datetime.now(UTC))

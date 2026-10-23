"""Generator competition: miner uploads and the public board (shadow).

``POST /generator/packs`` accepts a signed challenge pack. The checks run
cheap-before-expensive and every mutation happens after every check passes:
lane open, fresh domain-separated signature, ban, subnet registration, bytes
and identity, duplicate, per-hotkey pending cap, cooldown, then one insert.

Pack contents never leave Platform except to the trusted scorer. The listing
and board here expose identities, lifecycle, and aggregate outcomes only.
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
    GeneratorCaseStatusCounts,
    GeneratorPackListResponse,
    GeneratorPackSummary,
    GeneratorPackUploadRequest,
    GeneratorPackUploadResponse,
    pack_signature_payload,
)
from ditto.api_models.generator_ledger import GeneratorLedgerResponse
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.generator_competition import (
    PackRejected,
    build_ledger,
    inspect_pack,
    load_settings,
)
from ditto.chain.errors import ChainError
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.generator import (
    case_status_counts,
    count_pending_packs,
    get_pack_by_sha,
    insert_pack,
    latest_pack_submitted_at,
    list_packs,
)
from ditto.db.queries.scores import emission_owner

if TYPE_CHECKING:
    from ditto.chain.client import ChainClient
    from ditto.db.models import GeneratorPack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generator", tags=["generator"])

SIGNATURE_MAX_SKEW_SECONDS = 300
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated["ChainClient", Depends(get_chain_client)]


def _verify_pack_signature(payload: GeneratorPackUploadRequest) -> bool:
    if (
        payload.signature_nonce.version != 4
        or abs(time.time() - payload.signature_timestamp) > SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    message = pack_signature_payload(
        hotkey=payload.hotkey,
        pack_sha256=payload.pack_sha256,
        timestamp=payload.signature_timestamp,
        nonce=payload.signature_nonce,
    )
    signature = payload.signature.removeprefix("0x")
    try:
        keypair = bittensor.Keypair(ss58_address=payload.hotkey)
        return bool(keypair.verify(message, bytes.fromhex(signature)))
    except (ValueError, TypeError):
        return False


def pack_summary(
    row: GeneratorPack, counts: dict[str, int] | None
) -> GeneratorPackSummary:
    issues = (row.validation or {}).get("issues")
    return GeneratorPackSummary(
        pack_id=row.pack_id,
        miner_hotkey=row.miner_hotkey,
        pack_sha256=row.pack_sha256,
        generator_name=row.generator_name,
        generator_version=row.generator_version,
        case_count=row.case_count,
        status=row.status,  # type: ignore[arg-type]
        last_outcome=row.last_outcome,
        score=row.score,
        case_counts=GeneratorCaseStatusCounts(**counts) if counts else None,
        issue_count=len(issues) if isinstance(issues, list) else None,
        submitted_at=row.submitted_at,
        scored_at=row.scored_at,
    )


@router.post("/packs", response_model=GeneratorPackUploadResponse)
async def upload_pack(
    payload: GeneratorPackUploadRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> GeneratorPackUploadResponse:
    """Accept one signed challenge pack into the scoring queue."""
    resolved = await load_settings(session)
    settings = resolved.settings
    if settings.mode == "off":
        raise HTTPException(
            status_code=403, detail="the generator competition is not accepting packs"
        )
    if not _verify_pack_signature(payload):
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
        header = inspect_pack(
            pack_base64=payload.pack_base64,
            pack_sha256=payload.pack_sha256,
            hotkey=payload.hotkey,
        )
    except PackRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = await get_pack_by_sha(session, payload.pack_sha256)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"this exact pack was already uploaded as {existing.pack_id}",
        )
    if (
        await count_pending_packs(session, miner_hotkey=payload.hotkey)
        >= settings.max_pending_per_hotkey
    ):
        raise HTTPException(
            status_code=429,
            detail=(
                f"at most {settings.max_pending_per_hotkey} packs may wait for "
                "scoring per hotkey"
            ),
        )
    now = datetime.now(UTC)
    last = await latest_pack_submitted_at(session, miner_hotkey=payload.hotkey)
    if last is not None:
        retry_at = last + timedelta(seconds=settings.upload_cooldown_seconds)
        if retry_at > now:
            response.headers["Retry-After"] = str(
                int((retry_at - now).total_seconds()) + 1
            )
            raise HTTPException(
                status_code=429,
                detail=f"pack upload cooldown until {retry_at.isoformat()}",
                headers={"Retry-After": response.headers["Retry-After"]},
            )

    try:
        row = await insert_pack(
            session,
            miner_hotkey=payload.hotkey,
            owner_key=emission_owner(
                miner_hotkey=payload.hotkey, miner_coldkey=neuron.coldkey or None
            ),
            pack_sha256=payload.pack_sha256,
            pack_bytes=header.raw,
            case_count=header.case_count,
            generator_name=header.generator_name,
            generator_version=header.generator_version,
            now=now,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="this exact pack was uploaded concurrently"
        ) from exc
    logger.info(
        "generator pack accepted hotkey=%s pack_id=%s cases=%d",
        payload.hotkey,
        row.pack_id,
        row.case_count,
    )
    return GeneratorPackUploadResponse(
        pack_id=row.pack_id,
        pack_sha256=row.pack_sha256,
        status="queued",
        case_count=row.case_count,
    )


@router.get("/packs", response_model=GeneratorPackListResponse)
async def list_hotkey_packs(
    session: SessionDep,
    hotkey: Annotated[str, Query(pattern=_SS58)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> GeneratorPackListResponse:
    """A hotkey's recent packs: lifecycle and aggregate outcome only."""
    rows = await list_packs(session, miner_hotkey=hotkey, limit=limit)
    counts = await case_status_counts(session, [row.pack_id for row in rows])
    return GeneratorPackListResponse(
        packs=[pack_summary(row, counts.get(row.pack_id)) for row in rows]
    )


@router.get("/board", response_model=GeneratorLedgerResponse)
async def board(session: SessionDep, response: Response) -> GeneratorLedgerResponse:
    """The shadow generator ledger (each hotkey's best fresh pack)."""
    response.headers["Cache-Control"] = "public, max-age=30"
    resolved = await load_settings(session)
    return await build_ledger(session, resolved=resolved, now=datetime.now(UTC))

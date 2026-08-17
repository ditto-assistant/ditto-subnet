"""Miner-facing signed profile-picture upload and clear."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_avatar import (
    MinerAvatarClearRequest,
    MinerAvatarResponse,
    MinerAvatarSetRequest,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.hippius import HippiusClient
from ditto.api_server.miner_avatar import (
    MAX_AVATAR_BYTES,
    MinerAvatarRejected,
    check_freshness,
    clear_message,
    public_avatar_path,
    set_message,
    sniff_image,
    verify_signed_action,
)
from ditto.api_server.name_claim import expected_netuid
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey
from ditto.db.queries.miner_avatars import (
    delete_miner_avatar,
    get_miner_avatar,
    record_avatar_nonce,
    upsert_miner_avatar,
)

router = APIRouter(prefix="/miner-avatars", tags=["miner-avatars"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _require_hippius(request: Request) -> HippiusClient:
    client = getattr(request.app.state, "hippius", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="miner avatars are not configured on this deployment",
        )
    return client


@router.post("", response_model=MinerAvatarResponse, status_code=201)
async def set_miner_avatar(
    request: Request,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    payload: Annotated[str, Form()],
) -> MinerAvatarResponse:
    hippius = _require_hippius(request)
    try:
        body = MinerAvatarSetRequest.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400, detail="netuid does not match this deployment"
        )
    now = datetime.now(UTC)
    try:
        check_freshness(issued_at=body.issued_at, now=now)
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raw = await file.read(MAX_AVATAR_BYTES + 1)
    try:
        content_type, ext = sniff_image(raw)
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.miner_hotkey)
    try:
        verify_signed_action(
            payload=set_message(
                netuid=body.netuid,
                miner_hotkey=body.miner_hotkey,
                content_sha256=digest,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            hotkey=body.miner_hotkey,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
            signature=body.proof.signature,
            bound_coldkey=bound,
        )
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    object_key = f"avatars/{body.miner_hotkey}.{ext}"
    async with session.begin():
        existing = await get_miner_avatar(session, hotkey=body.miner_hotkey)
        try:
            await record_avatar_nonce(
                session, nonce=body.nonce, miner_hotkey=body.miner_hotkey, now=now
            )
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=400, detail="avatar nonce has already been used"
            ) from exc
        try:
            await hippius.put_object(
                key=object_key, body=raw, content_type=content_type
            )
            if existing is not None and existing.object_key != object_key:
                await hippius.delete_object(key=existing.object_key)
        except ObjectUploadFailedError as exc:
            raise HTTPException(
                status_code=502, detail="failed to store avatar image"
            ) from exc
        row = await upsert_miner_avatar(
            session,
            miner_hotkey=body.miner_hotkey,
            object_key=object_key,
            content_type=content_type,
            sha256=digest,
            nonce=body.nonce,
            now=now,
        )
        response = MinerAvatarResponse(
            miner_hotkey=row.miner_hotkey,
            avatar_url=public_avatar_path(row.miner_hotkey),
            content_type=row.content_type,
            sha256=row.sha256,
            updated_at=row.updated_at,
        )
    return response


@router.post("/clear", response_model=MinerAvatarResponse)
async def clear_miner_avatar(
    request: Request,
    session: SessionDep,
    body: MinerAvatarClearRequest,
) -> MinerAvatarResponse:
    hippius = _require_hippius(request)
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400, detail="netuid does not match this deployment"
        )
    now = datetime.now(UTC)
    try:
        check_freshness(issued_at=body.issued_at, now=now)
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.miner_hotkey)
    try:
        verify_signed_action(
            payload=clear_message(
                netuid=body.netuid,
                miner_hotkey=body.miner_hotkey,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            hotkey=body.miner_hotkey,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
            signature=body.proof.signature,
            bound_coldkey=bound,
        )
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deleted_key: str | None = None
    async with session.begin():
        try:
            await record_avatar_nonce(
                session, nonce=body.nonce, miner_hotkey=body.miner_hotkey, now=now
            )
            await session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=400, detail="avatar nonce has already been used"
            ) from exc
        row = await delete_miner_avatar(session, hotkey=body.miner_hotkey)
        if row is not None:
            deleted_key = row.object_key
    if deleted_key is not None:
        try:
            await hippius.delete_object(key=deleted_key)
        except ObjectUploadFailedError as exc:
            raise HTTPException(
                status_code=502, detail="failed to delete avatar image"
            ) from exc
    return MinerAvatarResponse(
        miner_hotkey=body.miner_hotkey,
        avatar_url=None,
        content_type=None,
        sha256=None,
        updated_at=now if row is not None else None,
    )

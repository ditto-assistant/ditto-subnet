"""Device-code miner login and session inspection."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_session import (
    MinerDevicePublicResponse,
    MinerDeviceStartRequest,
    MinerDeviceStartResponse,
    MinerDeviceStatusResponse,
    MinerLoginApproveRequest,
    MinerSessionView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.miner_session import (
    DEFAULT_SCOPES,
    GRANT_TTL,
    MinerSessionRejected,
    check_freshness,
    clamp_ttl_seconds,
    has_scope,
    hash_secret,
    login_command,
    login_message,
    new_token,
    new_user_code,
    normalize_user_code,
    parse_scopes,
    scopes_csv,
    validate_pkce,
    validate_redirect_uri,
    verify_signed_action,
)
from ditto.api_server.name_claim import expected_netuid
from ditto.db.models import MinerSession, MinerSessionToken
from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey
from ditto.db.queries.miner_sessions import (
    MinerGrantConflictError,
    create_device_grant,
    create_session,
    expire_stale_grant,
    get_device_grant_by_code,
    get_oauth_client,
    issue_session_token,
    lookup_session_token,
    record_login_nonce,
    revoke_session,
    session_remaining,
)
from ditto.db.queries.miner_sessions import (
    get_session as get_session_row,
)


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/miner-auth",
    tags=["miner-auth"],
    dependencies=[Depends(no_store)],
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _origin(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto")
    proto = (forwarded or request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def _network_for_command(request: Request) -> str:
    origin = _origin(request)
    if "localhost" in origin or "127.0.0.1" in origin:
        return "local"
    return "finney"


def _verification_uri(request: Request, user_code: str) -> tuple[str, str]:
    origin = _origin(request)
    uri = f"{origin}/#/reviews"
    complete = f"{uri}?{urlencode({'code': user_code})}"
    return uri, complete


def _session_view(row, *, now: datetime) -> MinerSessionView:
    return MinerSessionView(
        session_id=row.session_id,
        miner_hotkey=row.miner_hotkey,
        scopes=list(parse_scopes(row.scopes)),
        label=row.label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        expires_in=session_remaining(expires_at=row.expires_at, now=now),
    )


async def resolve_miner_session(
    request: Request, session: AsyncSession
) -> tuple[MinerSession, MinerSessionToken]:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="miner session required")
    token = header[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="miner session required")
    now = datetime.now(UTC)
    found = await lookup_session_token(session, token_hash=hash_secret(token), now=now)
    if found is None:
        raise HTTPException(
            status_code=401, detail="miner session is invalid or expired"
        )
    return found


def require_scope(row, needed: str) -> None:
    if not has_scope(row.scopes, needed):  # type: ignore[arg-type]
        raise HTTPException(
            status_code=403,
            detail=f"miner session is missing the {needed} scope",
        )


@router.post("/device", response_model=MinerDeviceStartResponse)
async def start_device(
    request: Request,
    session: SessionDep,
    body: MinerDeviceStartRequest | None = None,
) -> MinerDeviceStartResponse:
    payload = body or MinerDeviceStartRequest(
        scopes=list(DEFAULT_SCOPES),
        ttl_seconds=24 * 3600,
    )
    now = datetime.now(UTC)
    requested: list[str] = list(payload.scopes)
    if "read" not in requested:
        requested = ["read", *requested]
    try:
        scopes = scopes_csv(requested)
        ttl = clamp_ttl_seconds(payload.ttl_seconds)
        if payload.code_challenge is not None:
            validate_pkce(payload.code_challenge, label="code_challenge")
        if payload.redirect_uri is not None:
            validate_redirect_uri(payload.redirect_uri)
    except MinerSessionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.client_id is not None:
        client = await get_oauth_client(session, client_id=payload.client_id)
        if client is None:
            raise HTTPException(status_code=400, detail="unknown oauth client")
        if payload.redirect_uri not in client.redirect_uris:
            raise HTTPException(
                status_code=400, detail="redirect_uri is not registered"
            )
    poll_token = secrets.token_urlsafe(32)
    user_code = ""
    last_error: MinerGrantConflictError | None = None
    for _ in range(8):
        user_code = new_user_code()
        try:
            async with session.begin():
                await create_device_grant(
                    session,
                    user_code=user_code,
                    poll_token_hash=hash_secret(poll_token),
                    scopes=scopes,
                    ttl_seconds=ttl,
                    expires_at=now + GRANT_TTL,
                    oauth_client_id=payload.client_id,
                    redirect_uri=payload.redirect_uri,
                    state=payload.state,
                    code_challenge=payload.code_challenge,
                )
            last_error = None
            break
        except MinerGrantConflictError as exc:
            last_error = exc
            continue
    if last_error is not None or not user_code:
        raise HTTPException(status_code=503, detail="could not allocate a login code")
    uri, complete = _verification_uri(request, user_code)
    return MinerDeviceStartResponse(
        user_code=user_code,
        poll_token=poll_token,
        verification_uri=uri,
        verification_uri_complete=complete,
        expires_in=int(GRANT_TTL.total_seconds()),
        scopes=list(parse_scopes(scopes)),
        ttl_seconds=ttl,
        login_command=login_command(
            user_code=user_code, network=_network_for_command(request)
        ),
    )


@router.get("/device/{user_code}", response_model=MinerDevicePublicResponse)
async def public_device(
    user_code: str,
    request: Request,
    session: SessionDep,
) -> MinerDevicePublicResponse:
    now = datetime.now(UTC)
    try:
        code = normalize_user_code(user_code)
    except MinerSessionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with session.begin():
        grant = await get_device_grant_by_code(session, user_code=code)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown login code")
        grant = await expire_stale_grant(session, grant=grant, now=now)
        client = None
        if grant.oauth_client_id:
            client = await get_oauth_client(session, client_id=grant.oauth_client_id)
    remaining = max(0, int((grant.expires_at - now).total_seconds()))
    return MinerDevicePublicResponse(
        user_code=grant.user_code,
        grant_id=grant.grant_id,
        status=grant.status,  # type: ignore[arg-type]
        scopes=list(parse_scopes(grant.scopes)),
        ttl_seconds=grant.ttl_seconds,
        expires_in=remaining,
        login_command=login_command(
            user_code=grant.user_code, network=_network_for_command(request)
        ),
        miner_hotkey=grant.miner_hotkey,
        oauth=bool(grant.oauth_client_id),
        oauth_client_id=grant.oauth_client_id,
        oauth_client_name=None if client is None else client.client_name,
        redirect_uri=grant.redirect_uri,
    )


@router.post("/device/{user_code}/approve", response_model=MinerDeviceStatusResponse)
async def approve_device(
    user_code: str,
    session: SessionDep,
    body: MinerLoginApproveRequest,
) -> MinerDeviceStatusResponse:
    now = datetime.now(UTC)
    try:
        code = normalize_user_code(user_code)
        check_freshness(issued_at=body.issued_at, now=now)
    except MinerSessionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.netuid != expected_netuid():
        raise HTTPException(
            status_code=400, detail="netuid does not match this deployment"
        )
    issued_token: str | None = None
    async with session.begin():
        grant = await get_device_grant_by_code(session, user_code=code, lock=True)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown login code")
        grant = await expire_stale_grant(session, grant=grant, now=now)
        if grant.status != "pending":
            raise HTTPException(status_code=409, detail=f"login code is {grant.status}")
        bound = await get_bound_coldkey_for_hotkey(session, hotkey=body.miner_hotkey)
        try:
            verify_signed_action(
                payload=login_message(
                    netuid=body.netuid,
                    miner_hotkey=body.miner_hotkey,
                    user_code=grant.user_code,
                    grant_id=grant.grant_id,
                    ttl_seconds=grant.ttl_seconds,
                    scopes=parse_scopes(grant.scopes),
                    nonce=body.nonce,
                    issued_at=body.issued_at,
                    key_kind=body.proof.key_kind,
                    signer=body.proof.signer,
                    oauth_client_id=grant.oauth_client_id,
                    redirect_uri=grant.redirect_uri,
                ),
                hotkey=body.miner_hotkey,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                bound_coldkey=bound,
            )
        except MinerSessionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            await record_login_nonce(
                session,
                nonce=body.nonce,
                miner_hotkey=body.miner_hotkey,
                now=now,
            )
        except MinerGrantConflictError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        label = "mcp" if grant.oauth_client_id else "dashboard"
        row = await create_session(
            session,
            miner_hotkey=body.miner_hotkey,
            scopes=grant.scopes,
            label=label,
            expires_at=now + timedelta(seconds=grant.ttl_seconds),
            now=now,
        )
        issued_token = new_token()
        await issue_session_token(
            session,
            session_id=row.session_id,
            token_hash=hash_secret(issued_token),
            now=now,
        )
        grant.status = "approved"
        grant.approved_at = now
        grant.miner_hotkey = body.miner_hotkey
        grant.session_id = row.session_id
        grant_view = grant
        session_row = row
    return MinerDeviceStatusResponse(
        user_code=grant_view.user_code,
        status="approved",
        scopes=list(parse_scopes(grant_view.scopes)),
        ttl_seconds=grant_view.ttl_seconds,
        session=_session_view(session_row, now=now),
        access_token=issued_token,
        token_type="Bearer",
        continue_url=None,
        oauth=bool(grant_view.oauth_client_id),
    )


@router.get("/device/{user_code}/status", response_model=MinerDeviceStatusResponse)
async def poll_device(
    user_code: str,
    request: Request,
    session: SessionDep,
) -> MinerDeviceStatusResponse:
    return await _poll_device(
        user_code=user_code,
        request=request,
        session=session,
    )


@router.post("/device/{user_code}/status", response_model=MinerDeviceStatusResponse)
async def poll_device_post(
    user_code: str,
    request: Request,
    session: SessionDep,
) -> MinerDeviceStatusResponse:
    return await _poll_device(
        user_code=user_code,
        request=request,
        session=session,
    )


async def _poll_device(
    *,
    user_code: str,
    request: Request,
    session: AsyncSession,
) -> MinerDeviceStatusResponse:
    now = datetime.now(UTC)
    try:
        code = normalize_user_code(user_code)
    except MinerSessionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = request.headers.get("x-miner-poll-token")
    if token is None and request.method == "POST":
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("poll_token"), str):
            token = payload["poll_token"]
    issued_token: str | None = None
    async with session.begin():
        grant = await get_device_grant_by_code(session, user_code=code, lock=True)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown login code")
        grant = await expire_stale_grant(session, grant=grant, now=now)
        if token is None or grant.poll_token_hash is None:
            raise HTTPException(status_code=401, detail="poll token is invalid")
        if not secrets.compare_digest(grant.poll_token_hash, hash_secret(token)):
            raise HTTPException(status_code=401, detail="poll token is invalid")
        session_row = None
        if grant.session_id is not None:
            session_row = await get_session_row(session, session_id=grant.session_id)
        if (
            grant.status == "approved"
            and grant.consumed_at is None
            and session_row is not None
        ):
            issued_token = new_token()
            await issue_session_token(
                session,
                session_id=session_row.session_id,
                token_hash=hash_secret(issued_token),
                now=now,
            )
            grant.consumed_at = now
            if not grant.oauth_client_id:
                grant.status = "consumed"
    return MinerDeviceStatusResponse(
        user_code=grant.user_code,
        status=grant.status,  # type: ignore[arg-type]
        scopes=list(parse_scopes(grant.scopes)),
        ttl_seconds=grant.ttl_seconds,
        session=_session_view(session_row, now=now) if session_row else None,
        access_token=issued_token,
        token_type="Bearer" if issued_token else None,
        continue_url=None,
        oauth=bool(grant.oauth_client_id),
    )


@router.get("/session", response_model=MinerSessionView)
async def current_session(request: Request, session: SessionDep) -> MinerSessionView:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
    return _session_view(row, now=now)


@router.post("/session/revoke", status_code=204)
async def revoke_current_session(request: Request, session: SessionDep) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        await revoke_session(session, row=row, now=now)

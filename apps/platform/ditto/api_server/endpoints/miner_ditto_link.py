"""Link a miner hotkey to a Ditto account with Sign in with Ditto.

``/me/ditto-link/*`` needs a live miner session (the hotkey proof);
``/miner-auth/ditto/callback`` is the public OIDC redirect target, bound to
its attempt only by the hashed ``state``. The Ditto user id is read from the
verified id_token and nowhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_ditto_link import (
    MinerDittoLinkAttemptResponse,
    MinerDittoLinkResponse,
    MinerDittoLinkStartRequest,
    MinerDittoLinkStartResponse,
    MinerDittoLinkView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.ditto_link import (
    ATTEMPT_TTL_SECONDS,
    DittoLinkClient,
    DittoLinkRejected,
    code_challenge_s256,
    hash_state,
    new_code_verifier,
    new_nonce,
    new_state,
    safe_return_to,
    with_result,
)
from ditto.api_server.endpoints.miner_auth import (
    require_scope,
    resolve_miner_session,
)
from ditto.db.models import MinerDittoLink, MinerDittoLinkAttempt
from ditto.db.queries.attestation import get_bound_coldkey_for_hotkey
from ditto.db.queries.miner_ditto_links import (
    create_attempt,
    expire_stale_attempt,
    get_active_link,
    get_attempt,
    get_attempt_by_state,
    revoke_link,
    upsert_link,
)


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["miner-ditto-link"], dependencies=[Depends(no_store)])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _client(request: Request) -> DittoLinkClient:
    client = getattr(request.app.state, "ditto_link", None)
    if client is None or not client.enabled:
        raise HTTPException(
            status_code=503,
            detail="Ditto account linking is not configured on this deployment",
        )
    return client


def _link_view(row: MinerDittoLink) -> MinerDittoLinkView:
    return MinerDittoLinkView(
        miner_hotkey=row.miner_hotkey,
        ditto_user_id=row.ditto_user_id,
        ditto_email=row.ditto_email,
        miner_coldkey=row.miner_coldkey,
        linked_via=row.linked_via,  # type: ignore[arg-type]
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/me/ditto-link", response_model=MinerDittoLinkResponse)
async def current_link(request: Request, session: SessionDep) -> MinerDittoLinkResponse:
    client = getattr(request.app.state, "ditto_link", None)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        link = await get_active_link(session, hotkey=row.miner_hotkey)
    return MinerDittoLinkResponse(
        enabled=bool(client is not None and client.enabled),
        link=_link_view(link) if link else None,
    )


@router.post("/me/ditto-link/start", response_model=MinerDittoLinkStartResponse)
async def start_link(
    request: Request,
    session: SessionDep,
    body: MinerDittoLinkStartRequest | None = None,
) -> MinerDittoLinkStartResponse:
    client = _client(request)
    payload = body or MinerDittoLinkStartRequest()
    now = datetime.now(UTC)
    state = new_state()
    nonce = new_nonce()
    verifier = new_code_verifier()
    return_to = safe_return_to(client.config, payload.return_to)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        attempt = await create_attempt(
            session,
            miner_hotkey=row.miner_hotkey,
            session_id=row.session_id,
            state_hash=hash_state(state),
            nonce=nonce,
            code_verifier=verifier,
            client=payload.client,
            return_to=return_to,
            now=now,
            expires_at=now + timedelta(seconds=ATTEMPT_TTL_SECONDS),
        )
        attempt_id = attempt.attempt_id
    return MinerDittoLinkStartResponse(
        attempt_id=attempt_id,
        authorize_url=client.authorize_url(
            state=state, nonce=nonce, code_challenge=code_challenge_s256(verifier)
        ),
        expires_in=ATTEMPT_TTL_SECONDS,
    )


@router.get(
    "/me/ditto-link/attempts/{attempt_id}",
    response_model=MinerDittoLinkAttemptResponse,
)
async def link_attempt(
    attempt_id: UUID, request: Request, session: SessionDep
) -> MinerDittoLinkAttemptResponse:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        attempt = await get_attempt(session, attempt_id=attempt_id)
        if attempt is None or attempt.miner_hotkey != row.miner_hotkey:
            raise HTTPException(status_code=404, detail="unknown link attempt")
        attempt = await expire_stale_attempt(session, attempt=attempt, now=now)
        link = (
            await get_active_link(session, hotkey=row.miner_hotkey)
            if attempt.status == "linked"
            else None
        )
    return _attempt_view(attempt, link)


def _attempt_view(
    attempt: MinerDittoLinkAttempt, link: MinerDittoLink | None
) -> MinerDittoLinkAttemptResponse:
    authenticated = attempt.status in ("authenticated", "linked")
    return MinerDittoLinkAttemptResponse(
        attempt_id=attempt.attempt_id,
        status=attempt.status,  # type: ignore[arg-type]
        error=attempt.error,
        ditto_user_id=attempt.ditto_user_id if authenticated else None,
        ditto_email=attempt.ditto_email if authenticated else None,
        miner_hotkey=attempt.miner_hotkey,
        link=_link_view(link) if link else None,
    )


@router.post(
    "/me/ditto-link/attempts/{attempt_id}/confirm",
    response_model=MinerDittoLinkAttemptResponse,
)
async def confirm_link(
    attempt_id: UUID, request: Request, session: SessionDep
) -> MinerDittoLinkAttemptResponse:
    """Write the link. Only the holder of the miner session that started the
    attempt can do this, and only for an attempt Ditto has authenticated.

    The callback is reachable by whoever holds the authorize URL, so it must
    never pair an account with a hotkey on its own: an attacker could start an
    attempt for their hotkey and trick a victim into signing in on it. The
    pairing is confirmed here, by the hotkey side, after seeing who signed in.
    """
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        attempt = await get_attempt(session, attempt_id=attempt_id)
        if attempt is None or attempt.miner_hotkey != row.miner_hotkey:
            raise HTTPException(status_code=404, detail="unknown link attempt")
        attempt = await expire_stale_attempt(session, attempt=attempt, now=now)
        if attempt.status == "linked":
            link = await get_active_link(session, hotkey=row.miner_hotkey)
            return _attempt_view(attempt, link)
        if attempt.status != "authenticated" or not attempt.ditto_user_id:
            raise HTTPException(
                status_code=409,
                detail=f"link attempt is {attempt.status}, not awaiting confirmation",
            )
        if attempt.expires_at <= now:
            attempt.status = "expired"
            attempt.completed_at = now
            raise HTTPException(status_code=409, detail="link attempt has expired")
        coldkey = await get_bound_coldkey_for_hotkey(session, hotkey=row.miner_hotkey)
        link = await upsert_link(
            session,
            hotkey=row.miner_hotkey,
            ditto_user_id=attempt.ditto_user_id,
            ditto_email=attempt.ditto_email,
            miner_coldkey=coldkey,
            linked_via=attempt.client,
            session_id=row.session_id,
            now=now,
        )
        attempt.status = "linked"
        attempt.error = None
        attempt.completed_at = now
        await session.flush()
        view = _attempt_view(attempt, link)
    return view


@router.delete("/me/ditto-link", status_code=204)
async def unlink(request: Request, session: SessionDep) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        await revoke_link(session, hotkey=row.miner_hotkey, now=now)


@router.get("/miner-auth/ditto/callback", include_in_schema=False)
async def oidc_callback(
    request: Request,
    session: SessionDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Finish the authorization-code flow and send the browser back."""
    client = getattr(request.app.state, "ditto_link", None)
    fallback = client.config.return_url if client is not None else "/"
    if client is None or not client.enabled:
        return RedirectResponse(
            with_result(fallback, outcome="error", reason="linking is not configured"),
            status_code=302,
        )
    if not state:
        return RedirectResponse(
            with_result(fallback, outcome="error", reason="missing state"),
            status_code=302,
        )
    now = datetime.now(UTC)
    async with session.begin():
        attempt = await get_attempt_by_state(
            session, state_hash=hash_state(state), lock=True
        )
        if attempt is None:
            return RedirectResponse(
                with_result(fallback, outcome="error", reason="unknown link attempt"),
                status_code=302,
            )
        attempt = await expire_stale_attempt(session, attempt=attempt, now=now)
        return_to = attempt.return_to or fallback
        if attempt.status != "pending":
            return RedirectResponse(
                with_result(
                    return_to,
                    outcome="error",
                    reason=f"link attempt is {attempt.status}",
                ),
                status_code=302,
            )
        if error or not code:
            _fail(
                attempt,
                now,
                (error_description or error or "no authorization code")[:200],
            )
            return RedirectResponse(
                with_result(return_to, outcome="error", reason=attempt.error),
                status_code=302,
            )
        # Consume the attempt before the network call so a replayed callback
        # with the same state can never redeem twice.
        attempt.status = "failed"
        attempt.error = "in progress"
        attempt.completed_at = now
        await session.flush()

    try:
        identity = await client.exchange_code(
            code=code, code_verifier=attempt.code_verifier, nonce=attempt.nonce
        )
    except DittoLinkRejected as exc:
        async with session.begin():
            fresh = await get_attempt(session, attempt_id=attempt.attempt_id)
            if fresh is not None:
                _fail(fresh, now, str(exc)[:200])
        return RedirectResponse(
            with_result(return_to, outcome="error", reason=str(exc)),
            status_code=302,
        )

    async with session.begin():
        fresh = await get_attempt(session, attempt_id=attempt.attempt_id)
        if fresh is None:
            return RedirectResponse(
                with_result(return_to, outcome="error", reason="link attempt vanished"),
                status_code=302,
            )
        # Park the verified identity; the link itself is written only when the
        # holder of the miner session confirms the pairing (see confirm_link).
        fresh.status = "authenticated"
        fresh.error = None
        fresh.ditto_user_id = identity.user_id
        fresh.ditto_email = identity.email if identity.email_verified else None
        fresh.completed_at = None
        attempt_id = fresh.attempt_id
    return RedirectResponse(
        with_result(return_to, outcome="confirm", attempt=str(attempt_id)),
        status_code=302,
    )


def _fail(attempt: MinerDittoLinkAttempt, now: datetime, reason: str) -> None:
    attempt.status = "failed"
    attempt.error = reason
    attempt.completed_at = now

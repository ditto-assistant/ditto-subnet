"""Link a miner hotkey to a Ditto account with Sign in with Ditto.

``/me/ditto-link/*`` needs a live miner session (the hotkey proof);
``/miner-auth/ditto/callback`` is the public OIDC redirect target, bound to
its attempt only by the hashed ``state``. The Ditto user id is read from the
verified id_token and nowhere else.
"""

from __future__ import annotations

import hashlib
import html
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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


# How long the signed-in Ditto browser has to accept the hotkey after the
# callback verified who they are.
ACCEPT_TTL_SECONDS = 600
BINDING_COOKIE_PREFIX = "ditto_link_"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _binding_cookie_name(attempt_id: UUID) -> str:
    return BINDING_COOKIE_PREFIX + attempt_id.hex


def _accept_url(config_redirect_url: str) -> str:
    base = config_redirect_url
    if base.endswith("/callback"):
        base = base[: -len("/callback")]
    return base + "/accept"


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
    response: Response,
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
    # A dashboard attempt is bound to the browser that started it: the public
    # callback must present this HttpOnly cookie, so an authorize URL handed
    # to someone else cannot complete a dashboard-started attempt at all.
    binding = secrets.token_urlsafe(32) if payload.client == "dashboard" else None
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
        if binding is not None:
            attempt.browser_binding_hash = _sha256(binding)
            await session.flush()
        attempt_id = attempt.attempt_id
    if binding is not None:
        response.set_cookie(
            _binding_cookie_name(attempt_id),
            binding,
            max_age=ATTEMPT_TTL_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/api/v1/miner-auth/ditto",
        )
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
        if attempt.browser_binding_hash is not None:
            presented = request.cookies.get(_binding_cookie_name(attempt.attempt_id))
            if presented is None or not secrets.compare_digest(
                _sha256(presented), attempt.browser_binding_hash
            ):
                # A different browser finished a dashboard-started attempt:
                # someone was handed the authorize URL. Fail closed.
                _fail(attempt, now, "this sign-in was started in a different browser")
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
        # Park the verified identity as identity_verified: the person who just
        # signed in has NOT yet agreed to be paired with this hotkey. They must
        # accept on the page below (single-use token, short TTL); only then may
        # the miner session confirm. Without this, whoever holds the authorize
        # URL could bind a victim's account to the attacker's hotkey.
        fresh.status = "identity_verified"
        fresh.error = None
        fresh.ditto_user_id = identity.user_id
        fresh.ditto_email = identity.email if identity.email_verified else None
        fresh.completed_at = None
        accept_token = secrets.token_urlsafe(32)
        fresh.accept_token_hash = _sha256(accept_token)
        fresh.accept_expires_at = now + timedelta(seconds=ACCEPT_TTL_SECONDS)
        attempt_id = fresh.attempt_id
    accept_url = (
        f"{_accept_url(client.config.redirect_url)}"
        f"?attempt={attempt_id}&t={accept_token}"
    )
    return RedirectResponse(accept_url, status_code=302)


async def _load_acceptable_attempt(
    session: AsyncSession, *, attempt_id: UUID, token: str, now: datetime
) -> MinerDittoLinkAttempt | None:
    attempt = await get_attempt(session, attempt_id=attempt_id)
    if attempt is None:
        return None
    attempt = await expire_stale_attempt(session, attempt=attempt, now=now)
    if attempt.status != "identity_verified" or attempt.accept_token_hash is None:
        return None
    if not secrets.compare_digest(_sha256(token), attempt.accept_token_hash):
        return None
    return attempt


def _accept_page(
    *,
    attempt: MinerDittoLinkAttempt,
    coldkey: str | None,
    token: str,
    accept_base: str,
) -> str:
    who = html.escape(
        attempt.ditto_email or attempt.ditto_user_id or "your Ditto account"
    )
    hotkey = html.escape(attempt.miner_hotkey)
    cold = html.escape(coldkey) if coldkey else "unknown"
    q = f"attempt={attempt.attempt_id}&amp;t={html.escape(token)}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Link this hotkey to your Ditto account?</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;
padding:0 1rem;color:#111}}code{{font-size:.9em;word-break:break-all}}
.warn{{background:#fff4d6;padding:.75rem 1rem;border-radius:.5rem}}
button{{font:inherit;padding:.6rem 1.2rem;border-radius:.5rem;border:1px solid #333;
cursor:pointer}}.ok{{background:#111;color:#fff}}.no{{background:#fff}}</style></head>
<body>
<h1>Link this miner hotkey to your Ditto account?</h1>
<p>You just signed in to Ditto as <strong>{who}</strong>. A DittoBench miner asked to
attach <strong>their hotkey</strong> to that account:</p>
<p>Hotkey: <code>{hotkey}</code><br>Coldkey: <code>{cold}</code></p>
<p class="warn">Only continue if <strong>you</strong> started this from your own
DittoBench console or <code>ditto link-ditto</code> for a hotkey you control. If
someone sent you this sign-in link, choose <em>Not me</em>: linking would let their
miner attribute Router inference and Feedback Track credit to your account and, once
user billing is enabled, spend your Ditto credits.</p>
<form method="post" style="display:inline"
 action="{accept_base}?{q}&amp;decision=accept">
<button class="ok" type="submit">Yes, link my Ditto account to this hotkey</button>
</form>
<form method="post" style="display:inline;margin-left:.75rem"
 action="{accept_base}?{q}&amp;decision=decline">
<button class="no" type="submit">Not me</button></form>
<p><small>Nothing is linked until you choose. After you accept, the miner still has to
confirm the pairing from their own session.</small></p>
</body></html>"""


@router.get("/miner-auth/ditto/accept", response_class=HTMLResponse)
async def accept_link_page(
    request: Request, session: SessionDep, attempt: UUID, t: str
) -> HTMLResponse:
    """The Ditto-side half of the pairing: show WHICH hotkey wants this account."""
    client = _client(request)
    now = datetime.now(UTC)
    async with session.begin():
        row = await _load_acceptable_attempt(
            session, attempt_id=attempt, token=t, now=now
        )
        if row is None:
            return HTMLResponse(
                "<!doctype html><title>Link request not available</title>"
                "<p>This link request is not available: it was already answered, "
                "expired, or the link is invalid.</p>",
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        coldkey = await get_bound_coldkey_for_hotkey(session, hotkey=row.miner_hotkey)
        page = _accept_page(
            attempt=row,
            coldkey=coldkey,
            token=t,
            accept_base=_accept_url(client.config.redirect_url),
        )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.post("/miner-auth/ditto/accept")
async def accept_link_decide(
    request: Request, session: SessionDep, attempt: UUID, t: str, decision: str
) -> RedirectResponse:
    """Consume the single-use accept token: accept → authenticated, else failed."""
    client = _client(request)
    now = datetime.now(UTC)
    async with session.begin():
        row = await _load_acceptable_attempt(
            session, attempt_id=attempt, token=t, now=now
        )
        if row is None:
            raise HTTPException(status_code=404, detail="link request not available")
        return_to = row.return_to or client.config.return_url
        row.accept_token_hash = None
        if decision == "accept":
            row.status = "authenticated"
            row.user_accepted_at = now
            outcome = with_result(
                return_to, outcome="confirm", attempt=str(row.attempt_id)
            )
        else:
            _fail(row, now, "declined by the Ditto account holder")
            outcome = with_result(return_to, outcome="error", reason=row.error)
        await session.flush()
    return RedirectResponse(outcome, status_code=303)


def _fail(attempt: MinerDittoLinkAttempt, now: datetime, reason: str) -> None:
    attempt.status = "failed"
    attempt.error = reason
    attempt.completed_at = now

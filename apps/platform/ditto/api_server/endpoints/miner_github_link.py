"""A GitHub OAuth browser consent followed by a durable signed hotkey link."""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.contributor_identity import (
    GithubLinkAttemptStatus,
    GithubLinkRequest,
    GithubLinkStart,
    GithubLinkStatus,
    GithubLinkView,
    GithubUnlinkRequest,
    github_link_message,
    github_unlink_message,
)
from ditto.api_server.contributor_identity import (
    ContributorProofRejected,
    check_contributor_proof,
    finalized_owner,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.ditto_link import hash_state, new_code_verifier, new_state
from ditto.api_server.endpoints.miner_auth import require_scope, resolve_miner_session
from ditto.api_server.github_link import (
    ATTEMPT_TTL_SECONDS,
    GithubLinkClient,
    GithubLinkRejected,
)
from ditto.chain import ChainError
from ditto.db.models import (
    MinerGithubActionNonce,
    MinerGithubLink,
    MinerGithubLinkAttempt,
    MinerSession,
)
from ditto.db.queries.contributor_identity import (
    active_github_link,
    lock_identity_writes,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Private handlers declare Authorization for OpenAPI; resolve_miner_session
# validates that same Request header inside their transaction.


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["contributor-github"], dependencies=[Depends(no_store)])


def _client(request: Request) -> GithubLinkClient:
    client = request.app.state.github_link
    if not client.enabled:
        raise HTTPException(503, "GitHub linking is not configured on this deployment")
    return client


def link_view(row: MinerGithubLink) -> GithubLinkView:
    return GithubLinkView.model_validate(
        {name: getattr(row, name) for name in GithubLinkView.model_fields}
    )


def _page(
    title: str, content: str, *, status: int = 200, script_nonce: str | None = None
) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta '
        'name="viewport" content="width=device-width">'
        f"<title>{html.escape(title)}</title><body><main>"
        f"<h1>{html.escape(title)}</h1>{content}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
                + (f"; script-src 'nonce-{script_nonce}'" if script_nonce else "")
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _cookie(attempt_id: UUID) -> str:
    return "ditto_github_" + attempt_id.hex


def _expire(attempt: MinerGithubLinkAttempt) -> None:
    if attempt.expires_at <= datetime.now(UTC) and attempt.status not in (
        "linked",
        "failed",
        "expired",
    ):
        attempt.status = "expired"
        attempt.code_verifier = None
        attempt.accept_token_hash = None


def _has_binding(request: Request, attempt: MinerGithubLinkAttempt) -> bool:
    value = request.cookies.get(_cookie(attempt.attempt_id), "")
    return bool(
        value
        and attempt.browser_binding_hash
        and secrets.compare_digest(hash_state(value), attempt.browser_binding_hash)
    )


@router.get("/me/github-link", response_model=GithubLinkStatus)
async def my_link(
    request: Request,
    session: SessionDep,
    _authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> GithubLinkStatus:
    async with session.begin():
        miner, _ = await resolve_miner_session(request, session)
        require_scope(miner, "read")
        link = await active_github_link(
            session,
            hotkey=miner.miner_hotkey,
        )
    return GithubLinkStatus(
        netuid=request.app.state.config.chain.netuid,
        enabled=request.app.state.github_link.enabled,
        link=link_view(link) if link else None,
    )


@router.get("/public/github-link/{hotkey}", response_model=GithubLinkStatus)
async def public_link(
    hotkey: str, request: Request, session: SessionDep
) -> GithubLinkStatus:
    link = await active_github_link(session, hotkey=hotkey)
    return GithubLinkStatus(
        netuid=request.app.state.config.chain.netuid,
        enabled=request.app.state.github_link.enabled,
        link=link_view(link) if link else None,
    )


@router.get("/public/github-account/{github_user_id}", response_model=GithubLinkStatus)
async def account_link(
    github_user_id: int, request: Request, session: SessionDep
) -> GithubLinkStatus:
    link = await active_github_link(
        session,
        github_user_id=github_user_id,
    )
    return GithubLinkStatus(
        netuid=request.app.state.config.chain.netuid,
        enabled=request.app.state.github_link.enabled,
        link=link_view(link) if link else None,
    )


@router.post("/me/github-link/start", response_model=GithubLinkStart)
async def start_link(
    request: Request,
    session: SessionDep,
    _authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> GithubLinkStart:
    client = _client(request)
    launch = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    async with session.begin():
        miner, _ = await resolve_miner_session(request, session)
        require_scope(miner, "profile")
        await lock_identity_writes(
            session, namespace=f"github-link-start:{miner.miner_hotkey}"
        )
        pending = await session.scalar(
            select(func.count())
            .select_from(MinerGithubLinkAttempt)
            .where(
                MinerGithubLinkAttempt.hotkey == miner.miner_hotkey,
                MinerGithubLinkAttempt.netuid == request.app.state.config.chain.netuid,
                MinerGithubLinkAttempt.expires_at > now,
                MinerGithubLinkAttempt.status.in_(
                    ("pending", "identity_verified", "accepted")
                ),
            )
        )
        if pending and pending >= 5:
            raise HTTPException(
                429, "too many pending GitHub link attempts; wait for expiry"
            )
        attempt = MinerGithubLinkAttempt(
            netuid=request.app.state.config.chain.netuid,
            hotkey=miner.miner_hotkey,
            session_id=miner.session_id,
            launch_hash=hash_state(launch),
            status="pending",
            created_at=now,
            expires_at=now + timedelta(seconds=ATTEMPT_TTL_SECONDS),
        )
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.attempt_id
    # This launch token is NOT OAuth state. State and PKCE are created only in
    # the browser route, and never returned to the miner CLI.
    return GithubLinkStart(
        netuid=request.app.state.config.chain.netuid,
        attempt_id=attempt_id,
        authorize_url=f"{client.config.origin}/api/v1/miner-auth/github/authorize/{attempt_id}#launch={launch}",
        expires_in=ATTEMPT_TTL_SECONDS,
    )


@router.get("/miner-auth/github/authorize/{attempt_id}", include_in_schema=False)
async def launch_page(attempt_id: UUID) -> Response:
    del attempt_id  # GET validates the UUID but never reads or changes an attempt.
    # Fragments are never sent in HTTP request URLs. Clear browser history
    # immediately, then submit the one-use capability in a form body.
    nonce = secrets.token_urlsafe(24)
    script = (
        "const token = new URLSearchParams(location.hash.slice(1)).get('launch');"
        "history.replaceState(null, '', location.pathname);"
        "if (!token || !/^[A-Za-z0-9_-]{20,128}$/.test(token)) {"
        "document.getElementById('launch-status').textContent = "
        "'This launch link is missing or invalid. Start a new link from the CLI.';"
        "} else {"
        "const form = document.createElement('form');"
        "form.method = 'POST'; form.action = location.pathname;"
        "const input = document.createElement('input');"
        "input.type = 'hidden'; input.name = 'launch'; input.value = token;"
        "form.append(input); document.body.append(form); form.submit();"
        "}"
    )
    return _page(
        "Starting GitHub sign-in",
        '<p id="launch-status">Opening GitHub sign-in…</p>'
        "<noscript>Enable JavaScript to open this one-use sign-in link.</noscript>"
        f'<script nonce="{nonce}">{script}</script>',
        script_nonce=nonce,
    )


@router.post("/miner-auth/github/authorize/{attempt_id}", include_in_schema=False)
async def authorize(
    attempt_id: UUID,
    request: Request,
    session: SessionDep,
    launch: Annotated[str, Form(min_length=20, max_length=128)],
) -> Response:
    client = _client(request)
    async with session.begin():
        attempt = await session.get(
            MinerGithubLinkAttempt, attempt_id, with_for_update=True
        )
        if attempt is None or not secrets.compare_digest(
            attempt.launch_hash, hash_state(launch)
        ):
            return _page(
                "Invalid sign-in link",
                "<p>Start a new link from the CLI.</p>",
                status=400,
            )
        _expire(attempt)
        if attempt.status != "pending" or attempt.state_hash is not None:
            return _page(
                "Sign-in already started or expired",
                "<p>Start a new link from the CLI.</p>",
                status=409,
            )
        state, verifier, binding = (
            new_state(),
            new_code_verifier(),
            secrets.token_urlsafe(32),
        )
        attempt.state_hash, attempt.code_verifier = hash_state(state), verifier
        attempt.browser_binding_hash = hash_state(binding)
    response = RedirectResponse(
        client.authorize_url(state=state, verifier=verifier),
        status_code=302,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    response.set_cookie(
        _cookie(attempt_id),
        binding,
        max_age=ATTEMPT_TTL_SECONDS,
        httponly=True,
        secure=client.config.redirect_url.startswith("https://"),
        samesite="lax",
        path="/api/v1/miner-auth/github",
    )
    return response


@router.get("/miner-auth/github/callback", include_in_schema=False)
async def callback(
    request: Request,
    session: SessionDep,
    state: Annotated[str, Query(min_length=20, max_length=128)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: str | None = None,
) -> Response:
    client = _client(request)
    async with session.begin():
        attempt = await session.scalar(
            select(MinerGithubLinkAttempt)
            .where(MinerGithubLinkAttempt.state_hash == hash_state(state))
            .with_for_update()
        )
        if attempt is None or not _has_binding(request, attempt):
            return _page(
                "Invalid sign-in",
                "<p>Start a new link from the CLI in this browser.</p>",
                status=400,
            )
        _expire(attempt)
        if attempt.status != "pending" or not attempt.code_verifier:
            return _page(
                "Sign-in already used or expired",
                "<p>Start a new link from the CLI.</p>",
                status=409,
            )
        verifier, attempt.code_verifier = attempt.code_verifier, None
        attempt_id = attempt.attempt_id
    # Consuming the verifier commits before network I/O. A second callback
    # cannot exchange the code, and CLI polling retains access to the row.
    failure = None
    identity = None
    try:
        if error or not code:
            raise GithubLinkRejected("GitHub sign-in was declined")
        identity = await client.exchange(code=code, verifier=verifier)
    except GithubLinkRejected as exc:
        failure = str(exc)
    except Exception:
        # The verifier is consumed; persist a terminal failure without leaking
        # unexpected provider exception bodies, tokens or credentials.
        failure = "GitHub identity verification failed; start again"
    async with session.begin():
        attempt = await session.get(
            MinerGithubLinkAttempt,
            attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        if attempt is None:
            return _page("Invalid sign-in", "<p>Start a new link.</p>", status=400)
        _expire(attempt)
        if attempt.status != "pending":
            return _page(
                "Sign-in already used or expired",
                "<p>Start a new link from the CLI.</p>",
                status=409,
            )
        if failure is not None or identity is None:
            failure = failure or "GitHub identity verification failed; start again"
            attempt.status, attempt.error = "failed", failure
            return _page(
                "GitHub sign-in failed", f"<p>{html.escape(failure)}</p>", status=400
            )
        attempt.github_user_id, attempt.github_login = identity.user_id, identity.login
        attempt.status = "identity_verified"
        accept = secrets.token_urlsafe(32)
        attempt.accept_token_hash = hash_state(accept)
        content = (
            f"<p>GitHub verified "
            f"<strong>@{html.escape(identity.login)}</strong> "
            f"(account ID {identity.user_id}).</p>"
            f"<p>Link this account to hotkey "
            f"<code>{html.escape(attempt.hotkey)}</code>?</p>"
            "<p>This records a public contributor identity link. It does not "
            "change payments or grant access to repositories.</p>"
            '<form method="post" action="/api/v1/miner-auth/github/accept">'
            f'<input type="hidden" name="attempt_id" value="{attempt.attempt_id}">'
            f'<input type="hidden" name="accept_token" value="{accept}">'
            '<button name="decision" value="accept">Approve this hotkey</button> '
            '<button name="decision" value="cancel">Cancel</button></form>'
        )
        return _page("Confirm your GitHub link", content)


@router.post("/miner-auth/github/accept", include_in_schema=False)
async def accept(
    request: Request,
    session: SessionDep,
    attempt_id: Annotated[UUID, Form()],
    accept_token: Annotated[str, Form(min_length=20, max_length=128)],
    decision: Annotated[str, Form()],
) -> Response:
    client = _client(request)
    if request.headers.get("origin") != client.config.origin:
        return _page(
            "Consent failed",
            "<p>Start a new link from the CLI in this browser.</p>",
            status=403,
        )
    async with session.begin():
        attempt = await session.get(
            MinerGithubLinkAttempt, attempt_id, with_for_update=True
        )
        if attempt is None or not _has_binding(request, attempt):
            return _page(
                "Consent failed",
                "<p>Start a new link from the CLI in this browser.</p>",
                status=403,
            )
        _expire(attempt)
        if (
            attempt.status != "identity_verified"
            or not attempt.accept_token_hash
            or not secrets.compare_digest(
                hash_state(accept_token), attempt.accept_token_hash
            )
        ):
            return _page(
                "Consent expired or invalid",
                "<p>Start a new link from the CLI.</p>",
                status=409,
            )
        if decision not in ("accept", "cancel"):
            return _page(
                "Consent failed",
                "<p>Start a new link from the CLI and choose Approve or Cancel.</p>",
                status=400,
            )
        attempt.accept_token_hash = None
        attempt.status = "accepted" if decision == "accept" else "failed"
        attempt.error = (
            None if decision == "accept" else "GitHub account holder cancelled the link"
        )
    return _page(
        "GitHub link approved" if decision == "accept" else "GitHub link cancelled",
        "<p>Return to the CLI. An approved link is recorded only after you sign "
        "it with your miner key.</p>",
    )


@router.get(
    "/me/github-link/attempts/{attempt_id}", response_model=GithubLinkAttemptStatus
)
async def attempt_status(
    attempt_id: UUID,
    request: Request,
    session: SessionDep,
    _authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> GithubLinkAttemptStatus:
    async with session.begin():
        miner, _ = await resolve_miner_session(request, session)
        require_scope(miner, "read")
        attempt = await session.get(MinerGithubLinkAttempt, attempt_id)
        if attempt is None or attempt.session_id != miner.session_id:
            raise HTTPException(404, "unknown GitHub link attempt")
        expired = attempt.expires_at <= datetime.now(UTC) and attempt.status not in (
            "linked",
            "failed",
            "expired",
        )
        status = "expired" if expired else attempt.status
        accepted = status in ("accepted", "linked")
        return GithubLinkAttemptStatus(
            attempt_id=attempt_id,
            status=status,
            github_user_id=attempt.github_user_id if accepted else None,
            github_login=attempt.github_login if accepted else None,
            error=attempt.error,
        )


async def _authorize_action(
    request: Request,
    session: AsyncSession,
    body: GithubLinkRequest | GithubUnlinkRequest,
    *,
    now: datetime,
) -> MinerSession:
    miner, _ = await resolve_miner_session(request, session)
    require_scope(miner, "profile")
    action = "link" if isinstance(body, GithubLinkRequest) else "unlink"
    if (
        body.hotkey != miner.miner_hotkey
        or body.netuid != request.app.state.config.chain.netuid
    ):
        raise HTTPException(
            400, f"{action} hotkey or netuid does not match this miner session"
        )
    if isinstance(body, GithubLinkRequest):
        payload = github_link_message(
            netuid=body.netuid,
            hotkey=body.hotkey,
            github_user_id=body.github_user_id,
            github_login=body.github_login,
            link_attempt_id=body.link_attempt_id,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
        )
    else:
        payload = github_unlink_message(
            netuid=body.netuid,
            hotkey=body.hotkey,
            link_id=body.link_id,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
        )
    check_contributor_proof(
        payload=payload,
        hotkey=body.hotkey,
        key_kind=body.proof.key_kind,
        signer=body.proof.signer,
        signature=body.proof.signature,
        issued_at=body.issued_at,
        now=now,
    )
    # Resolve the session's activity writes before taking attempt/global locks.
    # Both actions must acquire those database locks in the same order.
    await session.flush()
    return miner


async def _approved_attempt(
    session: AsyncSession,
    body: GithubLinkRequest,
    miner: MinerSession,
    *,
    now: datetime,
    for_update: bool = False,
) -> MinerGithubLinkAttempt:
    attempt = await session.get(
        MinerGithubLinkAttempt,
        body.link_attempt_id,
        with_for_update=for_update,
        populate_existing=True,
    )
    if (
        attempt is None
        or attempt.session_id != miner.session_id
        or attempt.netuid != body.netuid
    ):
        raise HTTPException(404, "unknown GitHub link attempt")
    if (
        attempt.status != "accepted"
        or attempt.expires_at <= now
        or (
            body.github_user_id,
            body.github_login,
        )
        != (attempt.github_user_id, attempt.github_login)
    ):
        raise HTTPException(
            409, "the exact GitHub identity must be approved before signing"
        )
    return attempt


@router.post("/me/github-link/confirm", response_model=GithubLinkView)
async def confirm(
    body: GithubLinkRequest,
    request: Request,
    session: SessionDep,
    _authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> GithubLinkView:
    now = datetime.now(UTC)
    try:
        async with session.begin():
            miner = await _authorize_action(request, session, body, now=now)
            await _approved_attempt(session, body, miner, now=now)
        # Release the connection before RPCs. Expire the first-phase identity
        # map so session revocation cannot be hidden by cached ORM objects.
        session.expire_all()
        owner = await finalized_owner(request.app.state.chain, body.hotkey)
        if body.proof.key_kind == "coldkey" and body.proof.signer != owner.coldkey:
            raise ContributorProofRejected("signer is not the finalized Owner(hotkey)")
        async with session.begin():
            now = datetime.now(UTC)
            miner = await _authorize_action(request, session, body, now=now)
            attempt = await _approved_attempt(
                session, body, miner, now=now, for_update=True
            )
            await lock_identity_writes(session, namespace="github-link")
            now = datetime.now(UTC)
            previous = await active_github_link(session, hotkey=body.hotkey)
            if previous:
                previous.revoked_at = now
                await session.flush()
            link_id = uuid4()
            session.add(
                MinerGithubActionNonce(
                    nonce=body.nonce,
                    netuid=body.netuid,
                    hotkey=body.hotkey,
                    action="link",
                    issued_at=body.issued_at,
                    signature=body.proof.signature,
                    signer=body.proof.signer,
                    key_kind=body.proof.key_kind,
                    link_id=link_id,
                    created_at=now,
                )
            )
            await session.flush()
            link = MinerGithubLink(
                link_id=link_id,
                netuid=body.netuid,
                hotkey=body.hotkey,
                coldkey=owner.coldkey,
                github_user_id=body.github_user_id,
                github_login=body.github_login,
                link_attempt_id=body.link_attempt_id,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                linked_at=now,
                owner_block=owner.block,
                owner_block_hash=owner.block_hash,
            )
            session.add(link)
            attempt.status = "linked"
            await session.flush()
            view = link_view(link)
        return view
    except ContributorProofRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChainError:
        raise HTTPException(
            503, "finalized ownership unavailable; retry shortly"
        ) from None
    except IntegrityError:
        raise HTTPException(
            409, "nonce already used or GitHub account already linked to another hotkey"
        ) from None


@router.post("/me/github-link/unlink", status_code=204)
async def unlink(
    body: GithubUnlinkRequest,
    request: Request,
    session: SessionDep,
    _authorization: Annotated[str | None, Header(alias="authorization")] = None,
) -> None:
    now = datetime.now(UTC)
    try:
        async with session.begin():
            await _authorize_action(request, session, body, now=now)
            link = await active_github_link(session, hotkey=body.hotkey)
            if link is None or link.link_id != body.link_id:
                raise HTTPException(409, "unlink must name the exact active link")
        session.expire_all()
        owner = await finalized_owner(request.app.state.chain, body.hotkey)
        if body.proof.key_kind == "coldkey" and body.proof.signer != owner.coldkey:
            raise ContributorProofRejected("signer is not the finalized Owner(hotkey)")
        async with session.begin():
            now = datetime.now(UTC)
            await _authorize_action(request, session, body, now=now)
            await lock_identity_writes(session, namespace="github-link")
            link = await active_github_link(session, hotkey=body.hotkey)
            if link is None or link.link_id != body.link_id:
                raise HTTPException(409, "unlink must name the exact active link")
            session.add(
                MinerGithubActionNonce(
                    nonce=body.nonce,
                    netuid=body.netuid,
                    hotkey=body.hotkey,
                    action="unlink",
                    issued_at=body.issued_at,
                    signature=body.proof.signature,
                    signer=body.proof.signer,
                    key_kind=body.proof.key_kind,
                    link_id=body.link_id,
                    created_at=now,
                )
            )
            link.revoked_at = now
            await session.flush()
    except ContributorProofRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChainError:
        raise HTTPException(
            503, "finalized ownership unavailable; retry shortly"
        ) from None
    except IntegrityError:
        raise HTTPException(409, "nonce already used") from None

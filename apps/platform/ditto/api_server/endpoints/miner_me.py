"""Signed-in miner console: profile, avatar, submissions, reviews, commands."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_avatar import MinerAvatarResponse
from ditto.api_models.miner_logs import MinerHarnessLogsResponse
from ditto.api_models.miner_session import (
    MinerCommand,
    MinerMeResponse,
    MinerMeReviewsResponse,
    MinerProfileLinks,
    MinerProfileUpdateRequest,
    MinerSessionView,
    PublicMinerReview,
    PublicMinerSubmission,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.miner_auth import (
    require_scope,
    resolve_miner_session,
)
from ditto.api_server.endpoints.miner_logs import load_owned_agent_logs
from ditto.api_server.hippius import HippiusClient
from ditto.api_server.miner_avatar import (
    MAX_AVATAR_BYTES,
    MinerAvatarRejected,
    public_avatar_path,
    sniff_image,
)
from ditto.api_server.miner_session import (
    MinerSessionRejected,
    ditto_uvx_command,
    normalize_discord_handle,
    normalize_github_url,
    normalize_x_url,
    parse_scopes,
)
from ditto.api_server.name_claim import expected_netuid
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.db.models import AthReview
from ditto.db.queries.miner_avatars import (
    delete_miner_avatar,
    get_miner_avatar,
    record_avatar_nonce,
    upsert_miner_avatar,
)
from ditto.db.queries.miner_sessions import (
    get_profile,
    list_recent_agents_for_hotkey,
    list_reviews_for_hotkey,
    session_remaining,
    upsert_profile,
)
from ditto.db.queries.name_claims import active_handle_claims


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/me", tags=["miner-me"], dependencies=[Depends(no_store)])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


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


def _require_hippius(request: Request) -> HippiusClient:
    client = getattr(request.app.state, "hippius", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="miner avatars are not configured on this deployment",
        )
    return client


def _profile_links(row) -> MinerProfileLinks:
    if row is None:
        return MinerProfileLinks()
    return MinerProfileLinks(
        x_url=row.x_url,
        github_url=row.github_url,
        discord_handle=row.discord_handle,
    )


def _handle_for_hotkey(claims, hotkey: str):
    from ditto.api_models.name_claim import PublicNameHandle

    chosen = None
    for claim in claims.values():
        if claim.claimant_hotkey != hotkey:
            continue
        if claim.status == "upheld":
            chosen = claim
            break
        if chosen is None:
            chosen = claim
    if chosen is None:
        return None
    return PublicNameHandle(
        stem=chosen.name_stem,
        status="reserved" if chosen.status == "upheld" else chosen.status,
        claim_id=chosen.claim_id,
    )


def miner_commands(*, network: str, scopes: str) -> list[MinerCommand]:
    parsed = parse_scopes(scopes)
    commands: list[MinerCommand] = []
    if "upload" in parsed:
        commands.append(
            MinerCommand(
                action="upload",
                command=ditto_uvx_command(
                    network=network,
                    argv="upload --path ./dittobench-submission.tgz",
                ),
                reason="Uploading spends TAO and must be signed locally.",
                submit_url="/api/v1/upload/agent",
            )
        )
    if "handle" in parsed:
        commands.append(
            MinerCommand(
                action="name-claim",
                command=ditto_uvx_command(
                    network=network, argv="name claim --name <handle>"
                ),
                reason="Handle reservation is a signed one-shot action.",
                submit_url="/api/v1/name-claims",
            )
        )
    if "profile" in parsed:
        commands.append(
            MinerCommand(
                action="avatar-set",
                command=ditto_uvx_command(
                    network=network, argv="avatar set --file ./pfp.png"
                ),
                reason="You can also upload a picture from the signed-in dashboard.",
                submit_url="/api/v1/me/avatar",
            )
        )
    if "challenges" in parsed:
        commands.append(
            MinerCommand(
                action="dispute",
                command=(
                    "Use the submission page dispute form, or sign the "
                    "screening-dispute message printed there."
                ),
                reason="Appeals bind a hotkey signature to one rejected agent.",
                submit_url="/api/v1/public/agent/{agent_id}/dispute",
            )
        )
    return commands


def _network(request: Request) -> str:
    from ditto.api_server.endpoints.miner_auth import _network_for_command

    return _network_for_command(request)


@router.get("", response_model=MinerMeResponse)
async def me(request: Request, session: SessionDep) -> MinerMeResponse:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        profile = await get_profile(session, hotkey=row.miner_hotkey)
        avatar = await get_miner_avatar(session, hotkey=row.miner_hotkey)
        claims = await active_handle_claims(session, netuid=expected_netuid())
    return MinerMeResponse(
        session=_session_view(row, now=now),
        profile=_profile_links(profile),
        name_handle=_handle_for_hotkey(claims, row.miner_hotkey),
        avatar_url=public_avatar_path(row.miner_hotkey) if avatar else None,
        profile_url=f"/miner/{row.miner_hotkey}",
        commands=miner_commands(network=_network(request), scopes=row.scopes),
    )


@router.patch("", response_model=MinerMeResponse)
async def update_me(
    request: Request,
    session: SessionDep,
    body: MinerProfileUpdateRequest,
) -> MinerMeResponse:
    now = datetime.now(UTC)
    try:
        x_url = normalize_x_url(body.x_url)
        github_url = normalize_github_url(body.github_url)
        discord_handle = normalize_discord_handle(body.discord_handle)
    except MinerSessionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        existing = await get_profile(session, hotkey=row.miner_hotkey)
        dumped = body.model_dump(exclude_unset=True)
        profile = await upsert_profile(
            session,
            hotkey=row.miner_hotkey,
            x_url=x_url
            if "x_url" in dumped
            else (None if existing is None else existing.x_url),
            github_url=github_url
            if "github_url" in dumped
            else (None if existing is None else existing.github_url),
            discord_handle=discord_handle
            if "discord_handle" in dumped
            else (None if existing is None else existing.discord_handle),
            now=now,
        )
        avatar = await get_miner_avatar(session, hotkey=row.miner_hotkey)
        claims = await active_handle_claims(session, netuid=expected_netuid())
    return MinerMeResponse(
        session=_session_view(row, now=now),
        profile=_profile_links(profile),
        name_handle=_handle_for_hotkey(claims, row.miner_hotkey),
        avatar_url=public_avatar_path(row.miner_hotkey) if avatar else None,
        profile_url=f"/miner/{row.miner_hotkey}",
        commands=miner_commands(network=_network(request), scopes=row.scopes),
    )


@router.post("/avatar", response_model=MinerAvatarResponse, status_code=201)
async def set_my_avatar(
    request: Request,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> MinerAvatarResponse:
    hippius = _require_hippius(request)
    raw = await file.read(MAX_AVATAR_BYTES + 1)
    try:
        content_type, ext = sniff_image(raw)
    except MinerAvatarRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        existing = await get_miner_avatar(session, hotkey=row.miner_hotkey)
        object_key = f"avatars/{row.miner_hotkey}.{ext}"
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
        nonce = uuid4()
        try:
            await record_avatar_nonce(
                session, nonce=nonce, miner_hotkey=row.miner_hotkey, now=now
            )
            stored = await upsert_miner_avatar(
                session,
                miner_hotkey=row.miner_hotkey,
                object_key=object_key,
                content_type=content_type,
                sha256=digest,
                nonce=nonce,
                now=now,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=400, detail="avatar nonce has already been used"
            ) from exc
    return MinerAvatarResponse(
        miner_hotkey=stored.miner_hotkey,
        avatar_url=public_avatar_path(stored.miner_hotkey),
        content_type=stored.content_type,
        sha256=stored.sha256,
        updated_at=stored.updated_at,
    )


@router.delete("/avatar", response_model=MinerAvatarResponse)
async def clear_my_avatar(request: Request, session: SessionDep) -> MinerAvatarResponse:
    hippius = _require_hippius(request)
    now = datetime.now(UTC)
    deleted_key: str | None = None
    hotkey: str
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "profile")
        hotkey = row.miner_hotkey
        stored = await delete_miner_avatar(session, hotkey=hotkey)
        if stored is not None:
            deleted_key = stored.object_key
    if deleted_key is not None:
        try:
            await hippius.delete_object(key=deleted_key)
        except ObjectUploadFailedError as exc:
            raise HTTPException(
                status_code=502, detail="failed to delete avatar image"
            ) from exc
    return MinerAvatarResponse(
        miner_hotkey=hotkey,
        avatar_url=None,
        content_type=None,
        sha256=None,
        updated_at=now,
    )


@router.get("/agents/{agent_id}/harness-logs", response_model=MinerHarnessLogsResponse)
async def my_harness_logs(
    agent_id: UUID,
    request: Request,
    session: SessionDep,
) -> MinerHarnessLogsResponse:
    """Return this signed-in miner's harness diagnostics for one of their agents.

    Authenticated by the miner session, not a one-shot hotkey signature. The
    caller already proved possession of the hotkey at ``ditto login`` /
    dashboard sign-in; this only checks that the session's hotkey owns
    ``agent_id``. Unknown and other-miners' agents are the same 404.
    """
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        payload = await load_owned_agent_logs(
            session, hotkey=row.miner_hotkey, agent_id=agent_id
        )
    if payload is None:
        raise HTTPException(status_code=404, detail="no such agent for this hotkey")
    return payload


@router.get("/submissions", response_model=list[PublicMinerSubmission])
async def my_submissions(
    request: Request,
    session: SessionDep,
) -> list[PublicMinerSubmission]:
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        agents = await list_recent_agents_for_hotkey(
            session, hotkey=row.miner_hotkey, limit=50
        )
        avatar = await get_miner_avatar(session, hotkey=row.miner_hotkey)
    avatar_url = public_avatar_path(row.miner_hotkey) if avatar else None
    return [
        PublicMinerSubmission(
            agent_id=agent.agent_id,
            name=agent.name,
            status=str(agent.status),
            created_at=agent.created_at,
            avatar_url=avatar_url,
        )
        for agent in agents
    ]


@router.get("/reviews", response_model=MinerMeReviewsResponse)
async def my_reviews(request: Request, session: SessionDep) -> MinerMeReviewsResponse:
    now = datetime.now(UTC)
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        items = await list_reviews_for_hotkey(session, hotkey=row.miner_hotkey)
    reviews: list[PublicMinerReview] = []
    for _kind, item, agent in items:
        if isinstance(item, AthReview):
            reviews.append(
                PublicMinerReview(
                    kind="ath",
                    agent_id=agent.agent_id,
                    name=agent.name,
                    status=item.status,
                    opened_at=item.opened_at,
                    detail=item.original_reason,
                )
            )
        else:
            reviews.append(
                PublicMinerReview(
                    kind="dispute",
                    agent_id=agent.agent_id,
                    name=agent.name,
                    status=item.status,
                    opened_at=item.created_at,
                    detail=item.message,
                )
            )
    return MinerMeReviewsResponse(generated_at=now, reviews=reviews)


@router.get("/commands", response_model=list[MinerCommand])
async def my_commands(request: Request, session: SessionDep) -> list[MinerCommand]:
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
    return miner_commands(network=_network(request), scopes=row.scopes)

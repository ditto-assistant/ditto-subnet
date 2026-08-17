"""Hosted miner MCP and the OAuth 2.1 wrapper that opens the dashboard login."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.miner_auth import (
    _origin,
    _verification_uri,
    require_scope,
    resolve_miner_session,
)
from ditto.api_server.endpoints.miner_me import miner_commands
from ditto.api_server.miner_avatar import public_avatar_path
from ditto.api_server.miner_session import (
    DEFAULT_SCOPES,
    DEFAULT_TTL_SECONDS,
    GRANT_TTL,
    OAUTH_CODE_TTL,
    hash_secret,
    login_command,
    new_token,
    new_user_code,
    normalize_user_code,
    parse_scopes,
    scopes_csv,
)
from ditto.api_server.name_claim import expected_netuid
from ditto.db.queries.miner_avatars import get_miner_avatar
from ditto.db.queries.miner_sessions import (
    MinerGrantConflictError,
    consume_oauth_code,
    create_device_grant,
    create_oauth_client,
    create_oauth_code,
    expire_stale_grant,
    get_device_grant_by_code,
    get_oauth_client,
    get_profile,
    issue_session_token,
    list_recent_agents_for_hotkey,
    list_reviews_for_hotkey,
)
from ditto.db.queries.miner_sessions import (
    get_session as get_session_row,
)
from ditto.db.queries.name_claims import active_handle_claims

router = APIRouter(tags=["miner-mcp"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

MCP_NAME = "SN118 Miner"
MCP_VERSION = "1.0.0"
_TOOL_HELP = (
    "Hosted miner MCP. Reads and profile edits use the signed-in session. "
    "Actions that still need a hotkey signature return a ditto CLI command."
)


def _issuer(request: Request) -> str:
    return _origin(request)


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(
        data,
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
async def oauth_metadata(request: Request) -> JSONResponse:
    issuer = _issuer(request)
    return _json(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/mcp/oauth/authorize",
            "token_endpoint": f"{issuer}/mcp/oauth/token",
            "registration_endpoint": f"{issuer}/mcp/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(DEFAULT_SCOPES),
        }
    )


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource(request: Request) -> JSONResponse:
    issuer = _issuer(request)
    return _json(
        {
            "resource": f"{issuer}/mcp",
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(DEFAULT_SCOPES),
        }
    )


@router.get("/.well-known/mcp/server.json")
async def mcp_server_metadata(request: Request) -> JSONResponse:
    issuer = _issuer(request)
    return _json(
        {
            "name": MCP_NAME,
            "version": MCP_VERSION,
            "description": (
                "Miner console for SN118: profile, submissions, reviews, "
                "and CLI commands for signed actions."
            ),
            "url": f"{issuer}/mcp",
            "transport": "streamable-http",
        }
    )


@router.post("/mcp/oauth/register")
async def register_client(request: Request, session: SessionDep) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid client metadata")
    uris = body.get("redirect_uris") or []
    if (
        not isinstance(uris, list)
        or not uris
        or not all(isinstance(uri, str) for uri in uris)
    ):
        raise HTTPException(status_code=400, detail="redirect_uris is required")
    name = body.get("client_name") or "miner-mcp"
    if not isinstance(name, str) or not (1 <= len(name) <= 120):
        raise HTTPException(status_code=400, detail="invalid client_name")
    client_id = "mcp_" + secrets.token_urlsafe(18)
    now = datetime.now(UTC)
    async with session.begin():
        await create_oauth_client(
            session,
            client_id=client_id,
            client_name=name[:120],
            redirect_uris=list(uris),
            now=now,
        )
    return _json(
        {
            "client_id": client_id,
            "client_name": name[:120],
            "redirect_uris": list(uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status=201,
    )


@router.get("/mcp/oauth/authorize")
async def authorize(request: Request, session: SessionDep) -> Response:
    params = request.query_params
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    challenge = params.get("code_challenge")
    method = params.get("code_challenge_method") or "S256"
    raw_scope = params.get("scope") or scopes_csv(DEFAULT_SCOPES)
    if method != "S256" or not challenge:
        raise HTTPException(status_code=400, detail="PKCE S256 is required")
    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=400, detail="client_id and redirect_uri are required"
        )
    now = datetime.now(UTC)
    async with session.begin():
        client = await get_oauth_client(session, client_id=client_id)
        if client is None or redirect_uri not in client.redirect_uris:
            raise HTTPException(
                status_code=400, detail="unknown client or redirect_uri"
            )
        try:
            parts = raw_scope.replace(",", " ").split() or list(DEFAULT_SCOPES)
            csv = scopes_csv(parts)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for _ in range(8):
            user_code = new_user_code()
            try:
                await create_device_grant(
                    session,
                    user_code=user_code,
                    poll_token_hash=None,
                    scopes=csv,
                    ttl_seconds=DEFAULT_TTL_SECONDS,
                    expires_at=now + GRANT_TTL,
                    oauth_client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    code_challenge=challenge,
                )
                break
            except MinerGrantConflictError:
                continue
        else:
            raise HTTPException(
                status_code=503, detail="could not allocate a login code"
            )
    _uri, complete = _verification_uri(request, user_code)
    return RedirectResponse(
        complete, status_code=302, headers={"Cache-Control": "no-store"}
    )


@router.get("/mcp/oauth/complete")
async def complete_oauth(request: Request, session: SessionDep) -> Response:
    raw_code = request.query_params.get("code")
    if not raw_code:
        raise HTTPException(status_code=400, detail="missing login code")
    try:
        user_code = normalize_user_code(raw_code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = datetime.now(UTC)
    async with session.begin():
        grant = await get_device_grant_by_code(session, user_code=user_code, lock=True)
        if grant is None:
            raise HTTPException(status_code=404, detail="unknown login code")
        grant = await expire_stale_grant(session, grant=grant, now=now)
        if (
            grant.status != "approved"
            or grant.session_id is None
            or not grant.redirect_uri
        ):
            raise HTTPException(
                status_code=409, detail="login is not ready to continue"
            )
        auth_code = secrets.token_urlsafe(32)
        await create_oauth_code(
            session,
            code_hash=hash_secret(auth_code),
            grant_id=grant.grant_id,
            session_id=grant.session_id,
            redirect_uri=grant.redirect_uri,
            expires_at=now + OAUTH_CODE_TTL,
        )
        grant.status = "consumed"
        grant.consumed_at = now
        redirect = grant.redirect_uri
        state = grant.state
    query = {"code": auth_code}
    if state:
        query["state"] = state
    sep = "&" if "?" in redirect else "?"
    return RedirectResponse(
        redirect + sep + urlencode(query),
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _verify_pkce(challenge: str, verifier: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(encoded, challenge)


@router.post("/mcp/oauth/token")
async def issue_token(request: Request, session: SessionDep) -> JSONResponse:
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    code = str(form.get("code") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    verifier = str(form.get("code_verifier") or "")
    if grant_type != "authorization_code" or not code or not verifier:
        return _json({"error": "invalid_request"}, status=400)
    now = datetime.now(UTC)
    async with session.begin():
        from ditto.db.queries.miner_sessions import get_device_grant, get_oauth_code

        pending = await get_oauth_code(session, code_hash=hash_secret(code))
        if (
            pending is None
            or pending.consumed_at is not None
            or pending.expires_at <= now
        ):
            return _json({"error": "invalid_grant"}, status=400)
        if pending.redirect_uri != redirect_uri:
            return _json({"error": "invalid_grant"}, status=400)
        grant = await get_device_grant(session, grant_id=pending.grant_id)
        if grant is None or not grant.code_challenge:
            return _json({"error": "invalid_grant"}, status=400)
        if not _verify_pkce(grant.code_challenge, verifier):
            return _json({"error": "invalid_grant"}, status=400)
        consumed = await consume_oauth_code(
            session, code_hash=hash_secret(code), now=now
        )
        if consumed is None:
            return _json({"error": "invalid_grant"}, status=400)
        row = await get_session_row(session, session_id=consumed.session_id)
        if row is None or row.revoked_at is not None or row.expires_at <= now:
            return _json({"error": "invalid_grant"}, status=400)
        token = new_token()
        await issue_session_token(
            session,
            session_id=row.session_id,
            token_hash=hash_secret(token),
            now=now,
        )
        expires_in = max(0, int((row.expires_at - now).total_seconds()))
        scopes = row.scopes
    return _json(
        {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": scopes,
        }
    )


def _rpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
    }


def _tools() -> list[dict[str, Any]]:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    return [
        _tool("whoami", "Return the signed-in miner session.", empty),
        _tool(
            "get_my_profile",
            "Return the miner's public profile and socials.",
            empty,
        ),
        _tool(
            "update_my_profile",
            "Update X, GitHub, and Discord profile links using the session.",
            {
                "type": "object",
                "properties": {
                    "x_url": {"type": "string"},
                    "github_url": {"type": "string"},
                    "discord_handle": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
        _tool("list_my_submissions", "List this hotkey's submissions.", empty),
        _tool(
            "list_my_reviews",
            "List ATH reviews and screening disputes for this hotkey.",
            empty,
        ),
        _tool(
            "prepare_signed_action",
            "Return the ditto CLI command for a hotkey-signed action.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["upload", "name-claim", "avatar-set", "dispute"],
                    }
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
        _tool(
            "get_miner_mcp_help",
            "How this MCP authenticates and what it will not sign.",
            empty,
        ),
    ]


async def _call_tool(
    request: Request,
    session: AsyncSession,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    row, _token = await resolve_miner_session(request, session)
    if name == "whoami":
        require_scope(row, "read")
        return {
            "miner_hotkey": row.miner_hotkey,
            "scopes": list(parse_scopes(row.scopes)),
            "expires_at": row.expires_at.isoformat(),
            "label": row.label,
        }
    if name == "get_my_profile":
        require_scope(row, "read")
        profile = await get_profile(session, hotkey=row.miner_hotkey)
        avatar = await get_miner_avatar(session, hotkey=row.miner_hotkey)
        claims = await active_handle_claims(session, netuid=expected_netuid())
        handle = None
        for claim in claims.values():
            if claim.claimant_hotkey == row.miner_hotkey:
                handle = {"stem": claim.name_stem, "status": claim.status}
                break
        return {
            "miner_hotkey": row.miner_hotkey,
            "name_handle": handle,
            "avatar_url": public_avatar_path(row.miner_hotkey) if avatar else None,
            "profile": {
                "x_url": None if profile is None else profile.x_url,
                "github_url": None if profile is None else profile.github_url,
                "discord_handle": None if profile is None else profile.discord_handle,
            },
            "profile_url": f"{_origin(request)}/miner/{row.miner_hotkey}",
        }
    if name == "update_my_profile":
        from ditto.api_server.miner_session import (
            normalize_discord_handle,
            normalize_github_url,
            normalize_x_url,
        )
        from ditto.db.queries.miner_sessions import upsert_profile

        require_scope(row, "profile")
        from ditto.api_server.miner_session import MinerSessionRejected

        try:
            x_url = normalize_x_url(arguments.get("x_url"))
            github_url = normalize_github_url(arguments.get("github_url"))
            discord_handle = normalize_discord_handle(arguments.get("discord_handle"))
        except MinerSessionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        profile = await upsert_profile(
            session,
            hotkey=row.miner_hotkey,
            x_url=x_url,
            github_url=github_url,
            discord_handle=discord_handle,
            now=now,
        )
        return {
            "miner_hotkey": row.miner_hotkey,
            "profile": {
                "x_url": profile.x_url,
                "github_url": profile.github_url,
                "discord_handle": profile.discord_handle,
            },
        }
    if name == "list_my_submissions":
        require_scope(row, "read")
        agents = await list_recent_agents_for_hotkey(
            session, hotkey=row.miner_hotkey, limit=50
        )
        return [
            {
                "agent_id": str(agent.agent_id),
                "name": agent.name,
                "status": str(agent.status),
                "created_at": agent.created_at.isoformat(),
            }
            for agent in agents
        ]
    if name == "list_my_reviews":
        require_scope(row, "read")
        items = await list_reviews_for_hotkey(session, hotkey=row.miner_hotkey)
        out = []
        for kind, item, agent in items:
            opened = item.opened_at if kind == "ath" else item.created_at
            out.append(
                {
                    "kind": kind,
                    "agent_id": str(agent.agent_id),
                    "name": agent.name,
                    "status": item.status,
                    "opened_at": opened.isoformat(),
                }
            )
        return out
    if name == "prepare_signed_action":
        action = str(arguments.get("action") or "")
        commands = {
            item.action: item.model_dump()
            for item in miner_commands(
                network="local" if "localhost" in (_origin(request)) else "finney",
                scopes=row.scopes,
            )
        }
        if action not in commands:
            raise HTTPException(
                status_code=400,
                detail=f"no command for {action!r} with this session's scopes",
            )
        return commands[action]
    if name == "get_miner_mcp_help":
        return {
            "help": _TOOL_HELP,
            "login": login_command(user_code="ABCD-EFGH"),
            "dashboard": f"{_origin(request)}/#/reviews",
        }
    del now
    raise HTTPException(status_code=404, detail=f"unknown tool {name}")


@router.get("/mcp")
async def miner_mcp_probe() -> Response:
    return Response(status_code=405, headers={"Cache-Control": "no-store"})


@router.post("/mcp")
async def miner_mcp(request: Request, session: SessionDep) -> Response:
    if not (request.headers.get("authorization") or "").lower().startswith("bearer "):
        return Response(
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="miner", '
                    f'resource_metadata="{_issuer(request)}/.well-known/oauth-protected-resource"'
                ),
                "Cache-Control": "no-store",
            },
        )
    try:
        payload = await request.json()
    except Exception:
        return _json(_rpc_error(None, -32700, "parse error"), status=400)
    if not isinstance(payload, dict):
        return _json(_rpc_error(None, -32600, "invalid request"), status=400)
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if method == "initialize":
        return _json(
            _rpc_result(
                rpc_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": MCP_NAME, "version": MCP_VERSION},
                    "instructions": _TOOL_HELP,
                },
            )
        )
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return _json(_rpc_result(rpc_id, {"tools": _tools()}))
    if method == "ping":
        return _json(_rpc_result(rpc_id, {}))
    if method == "tools/call":
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        arguments = raw_args if isinstance(raw_args, dict) else {}
        try:
            async with session.begin():
                result = await _call_tool(request, session, name, arguments)
        except HTTPException as exc:
            return _json(
                _rpc_result(
                    rpc_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"error": exc.detail}),
                            }
                        ],
                        "isError": True,
                    },
                )
            )
        text = json.dumps(result, default=str)
        return _json(
            _rpc_result(
                rpc_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        )
    return _json(_rpc_error(rpc_id, -32601, f"unknown method {method}"), status=400)

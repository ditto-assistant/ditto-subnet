from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.miner_session import login_message


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _kp() -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri("//Alice")


async def test_device_login_sets_profile_and_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    started = await client.post(
        "/api/v1/miner-auth/device",
        json={"scopes": ["read", "profile"], "ttl_seconds": 3600},
    )
    assert started.status_code == 200
    body = started.json()
    user_code = body["user_code"]
    poll_token = body["poll_token"]
    public = await client.get(f"/api/v1/miner-auth/device/{user_code}")
    assert public.status_code == 200
    grant_id = public.json()["grant_id"]

    alice = _kp()
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = login_message(
        netuid=118,
        miner_hotkey=alice.ss58_address,
        user_code=user_code,
        grant_id=grant_id,
        ttl_seconds=3600,
        scopes="profile,read",
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    approved = await client.post(
        f"/api/v1/miner-auth/device/{user_code}/approve",
        json={
            "netuid": 118,
            "miner_hotkey": alice.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof": {
                "key_kind": "hotkey",
                "signer": alice.ss58_address,
                "signature": alice.sign(payload).hex(),
            },
        },
    )
    assert approved.status_code == 200, approved.text
    token = approved.json()["access_token"]
    assert token.startswith("ditto_ms_")

    me = await client.get("/api/v1/me", headers={"authorization": f"Bearer {token}"})
    assert me.status_code == 200
    patched = await client.patch(
        "/api/v1/me",
        headers={"authorization": f"Bearer {token}"},
        json={
            "x_url": "https://x.com/jupiter",
            "github_url": "https://github.com/jupiter",
            "discord_handle": "jupiter",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["profile"]["discord_handle"] == "jupiter"

    profile = await client.get(f"/api/v1/public/miners/{alice.ss58_address}")
    assert profile.status_code == 200
    assert profile.json()["profile"]["x_url"] == "https://x.com/jupiter"

    mcp = await client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert mcp.status_code == 200
    names = [tool["name"] for tool in mcp.json()["result"]["tools"]]
    assert "whoami" in names
    assert "get_my_harness_logs" in names
    assert "prepare_signed_action" in names

    command = await client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "prepare_signed_action",
                "arguments": {"action": "upload"},
            },
        },
    )
    assert command.status_code == 200
    assert command.json()["result"]["isError"] is True

    leaked = await client.get(
        f"/api/v1/miner-auth/device/{user_code}/status",
        params={"poll_token": poll_token},
    )
    assert leaked.status_code == 401
    polled = await client.post(
        f"/api/v1/miner-auth/device/{user_code}/status",
        json={"poll_token": poll_token},
    )
    assert polled.status_code == 200
    assert polled.json()["access_token"]


async def test_oauth_complete_requires_complete_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    verifier = "a" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    registered = await client.post(
        "/mcp/oauth/register",
        json={
            "client_name": "codex",
            "redirect_uris": ["http://127.0.0.1:8757/cb"],
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    authorized = await client.get(
        "/mcp/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:8757/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert authorized.status_code == 302
    location = authorized.headers["location"]
    assert "complete=" in location
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(location)
    query = parse_qs(parsed.fragment.split("?", 1)[-1])
    user_code = query["code"][0]
    complete_token = query["complete"][0]
    public = await client.get(f"/api/v1/miner-auth/device/{user_code}")
    assert public.status_code == 200
    assert public.json()["scopes"] == ["read"]
    grant_id = public.json()["grant_id"]
    alice = _kp()
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = login_message(
        netuid=118,
        miner_hotkey=alice.ss58_address,
        user_code=user_code,
        grant_id=grant_id,
        ttl_seconds=public.json()["ttl_seconds"],
        scopes=public.json()["scopes"],
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=alice.ss58_address,
        oauth_client_id=public.json()["oauth_client_id"],
        redirect_uri=public.json()["redirect_uri"],
    )
    approved = await client.post(
        f"/api/v1/miner-auth/device/{user_code}/approve",
        json={
            "netuid": 118,
            "miner_hotkey": alice.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof": {
                "key_kind": "hotkey",
                "signer": alice.ss58_address,
                "signature": alice.sign(payload).hex(),
            },
        },
    )
    assert approved.status_code == 200, approved.text
    stolen = await client.get(
        f"/mcp/oauth/complete?code={user_code}", follow_redirects=False
    )
    assert stolen.status_code == 405
    missing = await client.post("/mcp/oauth/complete", json={"code": user_code})
    assert missing.status_code == 401
    finished = await client.post(
        "/mcp/oauth/complete",
        json={"code": user_code, "complete": complete_token},
    )
    assert finished.status_code == 200, finished.text
    redirect_to = finished.json()["redirect_to"]
    assert redirect_to.startswith("http://127.0.0.1:8757/cb?code=")
    auth_code = redirect_to.rsplit("code=", 1)[-1]
    tokened = await client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://127.0.0.1:8757/cb",
            "code_verifier": verifier,
        },
    )
    assert tokened.status_code == 200, tokened.text
    assert tokened.json()["scope"] == "read"

from __future__ import annotations

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

    polled = await client.get(
        f"/api/v1/miner-auth/device/{user_code}/status",
        params={"poll_token": poll_token},
    )
    assert polled.status_code == 200
    assert polled.json()["access_token"]

"""End-to-end coverage for signed miner profile pictures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.miner_avatar import set_message
from ditto.db.models import Agent

_URL = "/api/v1/miner-avatars"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _MemoryHippius:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put_object(self, *, key: str, body: bytes, content_type: str) -> str:
        self.objects[key] = (body, content_type)
        return key

    async def get_object(self, *, key: str) -> bytes:
        return self.objects[key][0]

    async def delete_object(self, *, key: str) -> None:
        self.objects.pop(key, None)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.hippius = _MemoryHippius()


def _kp(uri: str) -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri(uri)


def _set_payload(signer: bittensor.Keypair, raw: bytes) -> tuple[dict, bytes]:
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    digest = hashlib.sha256(raw).hexdigest()
    payload = set_message(
        netuid=118,
        miner_hotkey=signer.ss58_address,
        content_sha256=digest,
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=signer.ss58_address,
    )
    body = {
        "netuid": 118,
        "miner_hotkey": signer.ss58_address,
        "nonce": str(nonce),
        "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "proof": {
            "key_kind": "hotkey",
            "signer": signer.ss58_address,
            "signature": signer.sign(payload).hex(),
        },
    }
    return body, raw


async def test_set_and_public_get(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    payload, raw = _set_payload(alice, _PNG)
    response = await client.post(
        _URL,
        data={"payload": json.dumps(payload)},
        files={"file": ("me.png", raw, "image/png")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["miner_hotkey"] == alice.ss58_address
    assert body["content_type"] == "image/png"
    assert body["avatar_url"] == f"/api/v1/public/miners/{alice.ss58_address}/avatar"

    fetched = await client.get(body["avatar_url"])
    assert fetched.status_code == 200
    assert fetched.content == raw
    assert fetched.headers["content-type"].startswith("image/png")


async def test_rejects_when_hippius_is_unset(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.hippius = None
    alice = _kp("//Alice")
    payload, raw = _set_payload(alice, _PNG)
    response = await client.post(
        _URL,
        data={"payload": json.dumps(payload)},
        files={"file": ("me.png", raw, "image/png")},
    )
    assert response.status_code == 503


async def test_rejects_replayed_nonce(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    payload, raw = _set_payload(alice, _PNG)
    first = await client.post(
        _URL,
        data={"payload": json.dumps(payload)},
        files={"file": ("me.png", raw, "image/png")},
    )
    assert first.status_code == 201
    second = await client.post(
        _URL,
        data={"payload": json.dumps(payload)},
        files={"file": ("me.png", raw, "image/png")},
    )
    assert second.status_code == 400
    assert "nonce" in second.text


async def test_public_leaderboard_includes_avatar_url(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    payload, raw = _set_payload(alice, _PNG)
    await client.post(
        _URL,
        data={"payload": json.dumps(payload)},
        files={"file": ("me.png", raw, "image/png")},
    )
    created = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=uuid4(),
                miner_hotkey=alice.ss58_address,
                name="avatar-miner",
                sha256=uuid4().hex + uuid4().hex,
                size_bytes=524288,
                status=AgentStatus.UPLOADED,
                created_at=created,
            )
        )

    activity = await client.get("/api/v1/public/activity")
    assert activity.status_code == 200
    entry = activity.json()["entries"][0]
    assert entry["avatar_url"] == f"/api/v1/public/miners/{alice.ss58_address}/avatar"

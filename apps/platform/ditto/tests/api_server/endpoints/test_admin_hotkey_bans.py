"""Contract tests for audited hotkey-level ban controls."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import BannedHotkey

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {
    "Authorization": f"Bearer {_ADMIN_TOKEN}",
    "X-Admin-Actor": "operator@example.com",
}
_HOTKEY = "5FKbkmKbJHTgsELVPigLJqbmovaviDN7dHZzX7UJ6xoqG4fx"
_OTHER = "5G11111111111111111111111111111111111111111111111"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        session.add_all(
            [
                BannedHotkey(hotkey=_HOTKEY, reason="benchmark emulation"),
                BannedHotkey(hotkey=_OTHER, reason="confirmed copy"),
            ]
        )


async def test_lists_and_reads_active_hotkey_bans(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed(session_maker)

    listing = await client.get("/api/v1/admin/hotkey-bans", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 2
    assert {row["hotkey"] for row in listing.json()["bans"]} == {_HOTKEY, _OTHER}

    exact = await client.get(f"/api/v1/admin/hotkey-bans/{_HOTKEY}", headers=_HEADERS)
    assert exact.status_code == 200, exact.text
    assert exact.json()["banned"] is True
    assert exact.json()["active_ban"]["reason"] == "benchmark emulation"
    assert exact.json()["history"] == []


async def test_unban_requires_fresh_guard_and_preserves_audit(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed(session_maker)
    exact = await client.get(f"/api/v1/admin/hotkey-bans/{_HOTKEY}", headers=_HEADERS)
    banned_at = exact.json()["active_ban"]["banned_at"]
    payload = {
        "expected_banned_at": banned_at,
        "reason": "allow rebuilt architecture to submit under current screening",
        "confirmation": f"UNBAN HOTKEY {_HOTKEY}",
    }

    wrong = dict(payload, confirmation="UNBAN HOTKEY wrong")
    rejected = await client.post(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}/unban",
        headers=_HEADERS,
        json=wrong,
    )
    assert rejected.status_code == 409

    response = await client.post(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}/unban",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["banned"] is False
    assert response.json()["action"]["actor"] == "operator@example.com"
    assert response.json()["action"]["previous_reason"] == "benchmark emulation"

    verified = await client.get(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}", headers=_HEADERS
    )
    assert verified.json()["banned"] is False
    assert verified.json()["active_ban"] is None
    assert verified.json()["history"][0]["reason"] == payload["reason"]

    repeated = await client.post(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}/unban",
        headers=_HEADERS,
        json=payload,
    )
    assert repeated.status_code == 409


async def test_unban_rejects_stale_timestamp_and_missing_actor(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _seed(session_maker)
    payload = {
        "expected_banned_at": "2026-08-18T00:00:00Z",
        "reason": "allow rebuilt architecture to submit under current screening",
        "confirmation": f"UNBAN HOTKEY {_HOTKEY}",
    }
    stale = await client.post(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}/unban",
        headers=_HEADERS,
        json=payload,
    )
    assert stale.status_code == 409

    missing_actor_headers = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
    missing_actor = await client.post(
        f"/api/v1/admin/hotkey-bans/{_HOTKEY}/unban",
        headers=missing_actor_headers,
        json=payload,
    )
    assert missing_actor.status_code == 422

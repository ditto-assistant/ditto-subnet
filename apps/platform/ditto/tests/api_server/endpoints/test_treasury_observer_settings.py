"""Exact historic observer reads never substitute defaults or expose audit rows."""

import hashlib
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.treasury_settings import TreasurySettings
from ditto.db.models import TreasurySettingsRevision
from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
    _HEADERS,
    _URL,
    _install,
)

pytestmark = pytest.mark.asyncio


def row(
    revision: int, settings: Any, checksum: str | None = None
) -> TreasurySettingsRevision:
    return TreasurySettingsRevision(
        revision=revision,
        parent_revision=revision - 1,
        settings=settings,
        checksum=checksum
        or hashlib.sha256(
            json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        reason="private operator audit reason",
        actor="private-operator@example.com",
    )


async def test_reads_old_pin_beyond_history_and_retains_raw_checksum(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    settings = TreasurySettings().model_dump(mode="json")
    settings["future_supported_elsewhere"] = {"opaque": [1, 2, 3]}
    async with session_maker() as session:
        session.add_all([row(i, settings) for i in range(1, 202)])
        await session.commit()
    ordinary = await client.get(_URL, headers=_HEADERS)
    assert ordinary.status_code == 200, ordinary.text
    assert len(ordinary.json()["history"]) == 200
    assert all(item["revision"] != 1 for item in ordinary.json()["history"])
    response = await client.get(f"{_URL}/revisions/1", headers=_HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "revision": 1,
        "settings": settings,
        "checksum": row(1, settings).checksum,
    }
    assert "private" not in response.text
    async with session_maker() as session:
        stored = await session.get(TreasurySettingsRevision, 1)
        assert stored is not None and stored.settings == settings
        assert stored.actor == "private-operator@example.com"


@pytest.mark.parametrize("kind", ["checksum", "semantics", "oversize"])
async def test_refuses_invalid_history_without_mutation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    kind: str,
) -> None:
    _install(app, session_maker)
    settings = TreasurySettings().model_dump(mode="json")
    if kind == "semantics":
        settings["mode"] = "active"
    if kind == "oversize":
        settings["future"] = "x" * 65_536
    original = row(1, settings, "a" * 64 if kind == "checksum" else None)
    async with session_maker() as session:
        session.add(original)
        await session.commit()
    response = await client.get(f"{_URL}/revisions/1", headers=_HEADERS)
    assert response.status_code == 409, response.text
    assert response.json()["message"] == "Historical treasury settings are invalid"
    assert set(response.json()) == {"message", "error_code", "request_id"}
    assert response.headers["cache-control"] == "no-store"
    async with session_maker() as session:
        stored = await session.get(TreasurySettingsRevision, 1)
        assert stored is not None
        assert stored.settings == settings and stored.checksum == original.checksum
        assert stored.actor == original.actor


async def test_exact_read_auth_missing_and_bounds(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(f"{_URL}/revisions/1")).status_code in {401, 403}
    assert (
        await client.get(f"{_URL}/revisions/1", headers=_HEADERS)
    ).status_code == 404
    for value in ["0", "-1", "2147483648", "1.1", "true"]:
        assert (
            await client.get(f"{_URL}/revisions/{value}", headers=_HEADERS)
        ).status_code == 422

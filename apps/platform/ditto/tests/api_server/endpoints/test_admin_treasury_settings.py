"""Treasury shadow policy remains revisioned and economically inert."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio
_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "/api/v1/admin/treasury-settings"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(revision: int = 0) -> dict:
    return {
        "expected_revision": revision,
        "settings": {
            "mode": "shadow",
            "maintenance_bps": 100,
            "gm_bps": 50,
            "treasury_hotkey": "reviewed-hotkey",
            "treasury_coldkey": "reviewed-coldkey",
            "gm_account_ref": "operator-reviewed-gm-account",
            "max_daily_outflow_rao": 100_000_000,
            "max_single_topup_rao": 25_000_000,
            "max_slippage_bps": 50,
        },
        "reason": "review both proposed allocations",
        "actor": "operator@example.com",
        "confirmation": "RECORD TREASURY SHADOW POLICY",
    }


async def test_defaults_and_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get(_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["revision"] == 0
    assert initial.json()["miner_bps"] == 10_000
    assert initial.json()["weight_effect"] == "none"

    created = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert created.status_code == 200, created.text
    assert created.json()["parent_revision"] == 0
    assert created.json()["checksum"]
    current = (await client.get(_URL, headers=_HEADERS)).json()
    assert current["revision"] == 1
    assert current["miner_bps"] == 9850
    assert current["weight_effect"] == "none"
    assert current["history"][0]["actor"] == "operator@example.com"

    stale = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert stale.status_code == 409
    assert len((await client.get(_URL, headers=_HEADERS)).json()["history"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"maintenance_bps": 450, "gm_bps": 100},
        {"treasury_hotkey": None},
        {"gm_account_ref": None},
        {"max_single_topup_rao": 200_000_000},
        {"max_slippage_bps": 501},
        {"mode": "active"},
    ],
)
async def test_refuses_unsafe_policy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    change: dict,
) -> None:
    _install(app, session_maker)
    payload = _payload()
    payload["settings"].update(change)
    response = await client.post(_URL, headers=_HEADERS, json=payload)
    assert response.status_code == 422, response.text


async def test_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code in {401, 403}

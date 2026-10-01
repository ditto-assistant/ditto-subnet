"""Treasury shadow policy remains revisioned and economically inert."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

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
    assert current["history"][0]["actor"] == "platform_admin_token"

    spoofed = _payload(1)
    spoofed["actor"] = "other-human@example.com"
    spoofed_response = await client.post(
        _URL,
        headers={**_HEADERS, "X-Admin-Actor": "claimed-human@example.com"},
        json=spoofed,
    )
    assert spoofed_response.status_code == 200, spoofed_response.text
    assert spoofed_response.json()["actor"] == "platform_admin_token"

    stale = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert stale.status_code == 409
    assert len((await client.get(_URL, headers=_HEADERS)).json()["history"]) == 2


async def test_ledger_readiness_is_read_only_and_never_funding_ready(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.get(f"{_URL}/ledger-readiness", headers=_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["configured_proposal"] is None
    assert result["observer_status"] == "disabled"
    assert result["observer_scope"] == "this_platform_process"
    assert result["latest_stored_epoch_index"] is None
    assert result["stored_shadow_pin"] is None
    assert result["offline_policy_verified"] is False
    assert result["weight_effect"] == "none"
    assert result["can_enforce_weights"] is False
    assert "producer_disabled" in result["blocking_reasons"]
    assert "no_epoch_pin" in result["blocking_reasons"]
    # A second read cannot create an epoch observation as a side effect.
    repeat = await client.get(f"{_URL}/ledger-readiness", headers=_HEADERS)
    assert repeat.status_code == 200
    assert repeat.json() == result


async def test_ledger_readiness_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(f"{_URL}/ledger-readiness")).status_code in {401, 403}


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


async def test_v2_service_wallets_are_shadow_only_and_v1_history_is_preserved(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    legacy = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert legacy.status_code == 200, legacy.text

    settings: dict[str, Any] = {
        "allocation_version": 2,
        "treasury_hotkey": ("5" + "a" * 47),
        "treasury_coldkey": ("5" + "b" * 47),
        "service_buckets": [
            {
                "bucket_id": "gm_credits",
                "purpose": "GM inference credit",
                "allocation_bps": 1000,
                "holding_coldkey": ("5" + "c" * 47),
                "service_account_ref": None,
            },
            {
                "bucket_id": "bitsec_audits",
                "purpose": "independent security audits",
                "allocation_bps": 0,
            },
            {
                "bucket_id": "bitcast_ads",
                "purpose": "advertising campaigns",
                "allocation_bps": 0,
            },
        ],
    }
    proposal = {
        "expected_revision": 1,
        "settings": settings,
        "reason": "propose separate service wallets",
        "confirmation": "RECORD TREASURY SHADOW POLICY",
    }
    created = await client.post(_URL, headers=_HEADERS, json=proposal)
    assert created.status_code == 200, created.text
    current = (await client.get(_URL, headers=_HEADERS)).json()
    assert current["miner_bps"] == 9000
    assert current["weight_effect"] == "none"
    assert current["effective"]["service_buckets"][0]["bucket_id"] == "gm_credits"
    assert current["history"][1]["settings"]["allocation_version"] == 1
    assert current["history"][1]["settings"]["gm_bps"] == 50

    invalid: list[dict[str, Any]] = [
        {"service_buckets": [settings["service_buckets"][0]] * 2},
        {
            "service_buckets": [
                settings["service_buckets"][0],
                {
                    **settings["service_buckets"][1],
                    "allocation_bps": 1,
                    "holding_coldkey": ("5" + "d" * 47),
                },
            ]
        },
        {
            "service_buckets": [
                settings["service_buckets"][0],
                {
                    **settings["service_buckets"][1],
                    "allocation_bps": 1,
                },
            ]
        },
        {
            "service_buckets": [
                settings["service_buckets"][0],
                {
                    **settings["service_buckets"][1],
                    "holding_coldkey": ("5" + "c" * 47),
                },
            ]
        },
        {"treasury_coldkey": ("5" + "c" * 47)},
        {"treasury_hotkey": None},
        {"gm_bps": 1},
        {"max_daily_outflow_rao": 1},
        {"mode": "active"},
    ]
    for change in invalid:
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json={
                **proposal,
                "expected_revision": 2,
                "settings": {**settings, **change},
            },
        )
        assert response.status_code == 422, (change, response.text)

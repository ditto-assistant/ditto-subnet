"""Operators can see how far the metagraph snapshot trails the chain (SN-463)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from ditto.api_server.dependencies import get_chain_client
from ditto.api_server.endpoints import admin_registration_freshness as endpoint
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.chain import (
    BlockInfo,
    ChainClient,
    ChainConfig,
    ChainConnectionError,
    ChainRegistration,
    ChainRegistrations,
    NeuronInfo,
    NeuronSnapshot,
)

_SNAPSHOT_BLOCK = 9_458_900
_CHAIN_BLOCK = 9_459_235


def _chain(config: ChainConfig) -> tuple[ChainClient, AsyncMock]:
    chain = ChainClient(config)
    chain._pylon_neuron_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value=NeuronSnapshot(
            block=_SNAPSHOT_BLOCK,
            neurons=(
                NeuronInfo(hotkey="5Previous", coldkey="5PreviousCk", uid=237, stake=0),
            ),
            observed_at=datetime(2026, 11, 8, 5, 8, 40, tzinfo=UTC),
            registrations_block=_SNAPSHOT_BLOCK,
        )
    )
    reader = AsyncMock(
        return_value=ChainRegistrations(
            netuid=118,
            block=_CHAIN_BLOCK,
            block_timestamp=1_794_114_516,
            provider="archive.example",
            observed_at=datetime(2026, 11, 8, 5, 8, 41, tzinfo=UTC),
            registrations=(
                ChainRegistration(uid=237, hotkey="5NewOccupant", coldkey="5NewCk"),
            ),
        )
    )
    chain.read_chain_registrations = reader  # type: ignore[method-assign]
    return chain, reader


def _app(chain: ChainClient, config: ChainConfig) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(admin_api_token="test-admin", chain=config)
    app.include_router(endpoint.router, prefix="/api/v1")
    app.dependency_overrides[get_chain_client] = lambda: chain
    return app


async def test_requires_admin_and_reports_the_correction() -> None:
    config = ChainConfig(
        pylon_url="http://pylon",
        netuid=118,
        open_access_token="token",
        metagraph_lag_check_seconds=300.0,
    )
    chain, reader = _chain(config)
    # Disable the background check this request would otherwise schedule.
    chain._metagraph_lag_checked_at[118] = chain._clock()
    registration = await chain.get_hotkey_registration("5NewOccupant", 118)
    assert registration.registered
    chain.get_finalized_block = AsyncMock(  # type: ignore[method-assign]
        return_value=BlockInfo(number=_CHAIN_BLOCK + 2, hash="0x" + "ab" * 32)
    )
    app = _app(chain, config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/api/v1/admin/registration-freshness")
        assert denied.status_code == 401
        app.dependency_overrides[require_admin] = lambda: None
        allowed = await client.get("/api/v1/admin/registration-freshness")

    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    body = allowed.json()
    assert body["netuid"] == 118
    assert body["snapshot_block"] == _SNAPSHOT_BLOCK
    assert body["finalized_block"] == _CHAIN_BLOCK + 2
    assert body["snapshot_lag_blocks"] == _CHAIN_BLOCK + 2 - _SNAPSHOT_BLOCK
    assert body["lag_reconcile_threshold_blocks"] == 120
    assert body["lag_check_interval_seconds"] == 300.0
    assert body["serving_registrations_block"] == _CHAIN_BLOCK
    assert body["chain_registrations"] == {
        "block": _CHAIN_BLOCK,
        "block_timestamp": "2026-11-08T05:08:36Z",
        "provider": "archive.example",
        "observed_at": "2026-11-08T05:08:41Z",
    }
    assert body["changes"] == [
        {"uid": 237, "pylon_hotkey": "5Previous", "chain_hotkey": "5NewOccupant"}
    ]
    # A read never starts another chain-storage registration read.
    reader.assert_awaited_once()


async def test_a_failed_head_read_still_shows_the_snapshot() -> None:
    config = ChainConfig(pylon_url="http://pylon", netuid=118, open_access_token="t")
    chain, reader = _chain(config)
    chain.get_finalized_block = AsyncMock(  # type: ignore[method-assign]
        side_effect=ChainConnectionError("headers unavailable")
    )
    app = _app(chain, config)
    app.dependency_overrides[require_admin] = lambda: None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = (await client.get("/api/v1/admin/registration-freshness")).json()

    assert body["snapshot_block"] == _SNAPSHOT_BLOCK
    assert body["finalized_block"] is None
    assert body["snapshot_lag_blocks"] is None
    assert body["finalized_block_error"] == "headers unavailable"
    assert body["chain_registrations"] is None
    assert body["changes"] == []
    reader.assert_not_awaited()


async def test_unreachable_pylon_is_503() -> None:
    config = ChainConfig(pylon_url="http://pylon", netuid=118, open_access_token="t")
    chain, _ = _chain(config)
    chain._pylon_neuron_snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=ChainConnectionError("pylon down")
    )
    app = _app(chain, config)
    app.dependency_overrides[require_admin] = lambda: None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/admin/registration-freshness")

    assert response.status_code == 503

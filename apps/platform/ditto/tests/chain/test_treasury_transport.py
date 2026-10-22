"""Treasury transport selection does not cache observed chain authority."""

import asyncio
from dataclasses import replace

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainTimeoutError,
)
from ditto.chain.models import ChainConfig


def make_chain_config():
    return ChainConfig(
        pylon_url="http://unused.invalid", netuid=118, open_access_token="test-only"
    )


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, "wss://entrypoint-finney.opentensor.ai:443"),
        (
            {
                "public_archive_rpc_urls": (
                    "wss://archive.example",
                    "wss://other.example",
                )
            },
            "wss://archive.example",
        ),
        (
            {
                "archive_rpc_url": "wss://configured.example",
                "archive_rpc_api_key": "test-key",
                "archive_rpc_auth_mode": "query",
                "public_archive_rpc_urls": ("wss://archive.example",),
            },
            "wss://configured.example?authorization=test-key",
        ),
        ({"subtensor_network": "ws://127.0.0.1:9944"}, "ws://127.0.0.1:9944"),
    ],
)
def test_treasury_uses_existing_archive_order_without_caching_authority(
    monkeypatch, overrides, expected
):
    calls = []
    marker = object()

    def substrate(url, *, runtime_cache):
        calls.append((url, runtime_cache))
        return marker

    monkeypatch.setattr("ditto.chain.client._treasury_substrate", substrate)
    chain = ChainClient(replace(make_chain_config(), **overrides))
    assert chain._new_treasury_substrate(archive=True) is marker
    assert chain._new_treasury_substrate(archive=True) is marker
    assert [url for url, _ in calls] == [expected, expected]
    # Only decoded metadata shares the existing cache. Every call constructs a
    # fresh request-local substrate; no identity or permission result is stored.
    assert calls[0][1] is calls[1][1]
    ChainClient(replace(make_chain_config(), **overrides))._new_treasury_substrate(
        archive=True
    )
    assert calls[2][1] is not calls[0][1]


def test_default_treasury_transport_still_uses_live_network(monkeypatch):
    calls = []
    marker = object()

    def substrate(url, *, runtime_cache):
        assert runtime_cache is not None
        calls.append(url)
        return marker

    monkeypatch.setattr("ditto.chain.client._treasury_substrate", substrate)
    config = replace(
        make_chain_config(), public_archive_rpc_urls=("wss://archive.example",)
    )
    assert ChainClient(config)._new_treasury_substrate() is marker
    assert calls == ["wss://entrypoint-finney.opentensor.ai:443"]


@pytest.mark.asyncio
async def test_epoch_schedule_fails_over_from_refused_archive_to_next_provider(
    monkeypatch,
):
    attempts = []
    schedule = object()

    async def read(_self, url, _netuid):
        attempts.append(url)
        if url == "wss://archive.example":
            raise ConnectionError("server rejected WebSocket connection: HTTP 429")
        return schedule

    monkeypatch.setattr(ChainClient, "_read_epoch_schedule_from", read)
    config = replace(
        make_chain_config(),
        public_archive_rpc_urls=("wss://archive.example", "wss://other.example"),
    )
    assert await ChainClient(config).read_epoch_schedule(118) is schedule
    assert attempts == ["wss://archive.example", "wss://other.example"]


@pytest.mark.asyncio
async def test_epoch_schedule_reaches_live_route_after_every_archive_fails(
    monkeypatch,
):
    attempts = []

    async def read(_self, url, _netuid):
        attempts.append(url)
        raise ConnectionError(f"synthetic refusal from {url}")

    monkeypatch.setattr(ChainClient, "_read_epoch_schedule_from", read)
    config = replace(
        make_chain_config(), public_archive_rpc_urls=("wss://archive.example",)
    )
    with pytest.raises(ChainConnectionError, match="archive.example.*entrypoint"):
        await ChainClient(config).read_epoch_schedule(118)
    assert attempts == [
        "wss://archive.example",
        "wss://entrypoint-finney.opentensor.ai:443",
    ]


@pytest.mark.asyncio
async def test_epoch_schedule_bounds_each_provider_and_reports_timeouts(monkeypatch):
    async def read(_self, _url, _netuid):
        await asyncio.sleep(10)

    monkeypatch.setattr(ChainClient, "_read_epoch_schedule_from", read)
    config = replace(
        make_chain_config(),
        public_archive_rpc_urls=("wss://archive.example",),
        archive_rpc_timeout_seconds=0.01,
    )
    with pytest.raises(ChainTimeoutError):
        await ChainClient(config).read_epoch_schedule(118)


@pytest.mark.asyncio
async def test_epoch_schedule_fails_over_from_one_providers_typed_refusal(monkeypatch):
    schedule = object()

    async def read(_self, url, _netuid):
        if url == "wss://archive.example":
            raise ChainAuthError("synthetic rejected archive key")
        return schedule

    monkeypatch.setattr(ChainClient, "_read_epoch_schedule_from", read)
    config = replace(
        make_chain_config(), public_archive_rpc_urls=("wss://archive.example",)
    )
    assert await ChainClient(config).read_epoch_schedule(118) is schedule


@pytest.mark.asyncio
async def test_epoch_schedule_keeps_a_typed_error_every_provider_agreed_on(
    monkeypatch,
):
    async def read(_self, url, _netuid):
        raise ChainAuthError(f"synthetic refusal from {url}")

    monkeypatch.setattr(ChainClient, "_read_epoch_schedule_from", read)
    config = replace(
        make_chain_config(), public_archive_rpc_urls=("wss://archive.example",)
    )
    with pytest.raises(ChainAuthError, match="entrypoint-finney"):
        await ChainClient(config).read_epoch_schedule(118)

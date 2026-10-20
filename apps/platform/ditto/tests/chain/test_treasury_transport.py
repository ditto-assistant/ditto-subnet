"""Treasury transport selection does not cache observed chain authority."""

from dataclasses import replace

import pytest

from ditto.chain.client import ChainClient
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
    assert chain._new_treasury_substrate() is marker
    assert chain._new_treasury_substrate() is marker
    assert [url for url, _ in calls] == [expected, expected]
    # Only decoded metadata shares the existing cache. Every call constructs a
    # fresh request-local substrate; no identity or permission result is stored.
    assert calls[0][1] is calls[1][1]
    ChainClient(replace(make_chain_config(), **overrides))._new_treasury_substrate()
    assert calls[2][1] is not calls[0][1]

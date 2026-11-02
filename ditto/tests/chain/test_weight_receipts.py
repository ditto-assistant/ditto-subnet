"""Transport must distinguish known unsupported from ambiguous acceptance."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import ChainConnectionError, ChainTimeoutError
from ditto.chain.models import ChainConfig


@pytest.mark.parametrize(
    "status,detail,allowed",
    [
        (404, "missing", True),
        (503, "unacknowledged receipt capacity reached", True),
        (503, "database failed", False),
        (409, "conflict", False),
    ],
)
async def test_only_explicit_rejection_allows_legacy(
    monkeypatch, status, detail, allowed
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.request.return_value = httpx.Response(status, json={"detail": detail})
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    chain = ChainClient(
        ChainConfig(
            pylon_url="http://pylon",
            netuid=118,
            identity_name="validator",
            identity_token="fake",
        )
    )
    if allowed:
        assert await chain.put_weights_with_receipt("request", {}) is None
    else:
        with pytest.raises(ChainConnectionError):
            await chain.put_weights_with_receipt("request", {})


async def test_timeout_never_returns_unsupported(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.request.side_effect = httpx.ReadTimeout("lost response")
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    chain = ChainClient(
        ChainConfig(
            pylon_url="http://pylon",
            netuid=118,
            identity_name="validator",
            identity_token="fake",
        )
    )
    with pytest.raises(ChainTimeoutError):
        await chain.put_weights_with_receipt("request", {})

"""Exercise real SDK header helpers without its metadata startup task."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import async_substrate_interface
import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainConfig


@pytest.mark.parametrize(
    ("method", "args", "expected_methods"),
    [
        ("get_block_hash", (123,), ["chain_getBlockHash"]),
        (
            "get_finalized_block",
            (),
            ["chain_getFinalizedHead", "chain_getHeader"],
        ),
        (
            "get_finalized_block_hash",
            (123,),
            ["chain_getFinalizedHead", "chain_getHeader", "chain_getBlockHash"],
        ),
    ],
)
async def test_fresh_header_reads_skip_runtime(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[int, ...],
    expected_methods: list[str],
) -> None:
    sdk = async_substrate_interface.AsyncSubstrateInterface
    clients: list[Any] = []

    def factory(*, url: str) -> Any:
        client: Any = sdk(url=url, _mock=True)
        client.initialize = AsyncMock(side_effect=AssertionError("metadata startup"))
        client.init_runtime = AsyncMock(side_effect=AssertionError("runtime decode"))

        async def rpc(method: str, params: list[Any]) -> dict[str, Any]:
            if method == "chain_getFinalizedHead":
                assert params == []
                return {"result": f"0xhead{len(clients)}"}
            if method == "chain_getHeader":
                assert params == [f"0xhead{len(clients)}"]
                return {"result": {"number": hex(456 + len(clients))}}
            if method == "chain_getBlockHash":
                assert params == [123]
                return {"result": f"0xhash{len(clients)}"}
            raise AssertionError(method)

        client.rpc_request = AsyncMock(side_effect=rpc)
        clients.append(client)
        return client

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", factory)
    chain = ChainClient(
        ChainConfig(pylon_url="http://pylon", netuid=118, open_access_token="test")
    )
    first = await getattr(chain, method)(*args)
    second = await getattr(chain, method)(*args)
    assert first != second  # Even equal requested heights are read anew.
    assert len(clients) == 2
    for client in clients:
        assert [
            call.args[0] for call in client.rpc_request.await_args_list
        ] == expected_methods
        client.initialize.assert_not_awaited()
        client.init_runtime.assert_not_awaited()
        assert client.startup_runtime_task is None
        client.ws.connect.assert_awaited_once()
        client.ws.shutdown.assert_awaited_once()


@pytest.mark.parametrize("failure_at", ["connect", "read", "cancel"])
async def test_header_failure_closes_transport(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    sdk = async_substrate_interface.AsyncSubstrateInterface
    client: Any = sdk(url="ws://127.0.0.1:9944", _mock=True)
    if failure_at == "connect":
        client.ws.connect.side_effect = OSError("private provider detail")
    else:
        client.rpc_request = AsyncMock(
            side_effect=asyncio.CancelledError()
            if failure_at == "cancel"
            else OSError()
        )
    monkeypatch.setattr(
        async_substrate_interface, "AsyncSubstrateInterface", lambda **_kwargs: client
    )
    chain = ChainClient(
        ChainConfig(pylon_url="http://pylon", netuid=118, open_access_token="test")
    )
    expected = (
        asyncio.CancelledError if failure_at == "cancel" else ChainConnectionError
    )
    with pytest.raises(expected):
        await chain.get_finalized_block()
    client.ws.shutdown.assert_awaited_once()
    assert client.startup_runtime_task is None

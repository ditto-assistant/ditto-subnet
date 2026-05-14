"""Unit tests for ditto.chain.client.ChainClient."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
    ChainConnectionError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.models import ChainConfig
from ditto.tests.chain.conftest import make_event_record


def make_chain_config(**overrides: Any) -> ChainConfig:
    defaults: dict[str, Any] = dict(
        pylon_url="http://pylon:8080",
        identity_name="validator",
        identity_token="token",
        netuid=118,
    )
    defaults.update(overrides)
    return ChainConfig(**defaults)


def make_pylon_neuron(**overrides: Any) -> MagicMock:
    """Build a Pylon-shaped neuron object suitable for the adapter."""
    defaults: dict[str, Any] = dict(
        hotkey="5HK1",
        coldkey="5CK1",
        uid=0,
        stake=2.5,
        axon_info={"ip": "1.2.3.4"},
        registered_at_block=100,
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


def make_pylon_extrinsic(**overrides: Any) -> MagicMock:
    """Build a Pylon-shaped extrinsic object suitable for the adapter."""
    call = MagicMock(
        call_module=overrides.pop("call_module", "Balances"),
        call_function=overrides.pop("call_function", "transfer_keep_alive"),
        call_args=overrides.pop("call_args", {"value": 1000}),
    )
    defaults: dict[str, Any] = dict(
        block_hash=overrides.pop("block_hash", "0xabc"),
        address=overrides.pop("address", "5Signer"),
    )
    defaults.update(overrides)
    return MagicMock(call=call, **defaults)


class TestChainClientLifecycle:
    async def test_aenter_connects(self, install_pylon_module: AsyncMock):
        async with ChainClient(make_chain_config()) as client:
            assert client._pylon is install_pylon_module
        install_pylon_module.__aexit__.assert_awaited()

    async def test_aenter_wraps_failure(self, monkeypatch: pytest.MonkeyPatch):
        import sys

        module = MagicMock()
        module.AsyncPylonClient = MagicMock(side_effect=ConnectionError("nope"))
        monkeypatch.setitem(sys.modules, "pylon_client", module)
        with pytest.raises(ChainConnectionError):
            async with ChainClient(make_chain_config()):
                pass

    async def test_methods_require_async_with(self):
        client = ChainClient(make_chain_config())
        with pytest.raises(RuntimeError):
            await client.get_latest_block()


class TestGetRecentNeurons:
    async def test_returns_neuron_info_list(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = [
            make_pylon_neuron()
        ]
        async with ChainClient(make_chain_config()) as client:
            neurons = await client.get_recent_neurons(118)
        assert len(neurons) == 1
        assert neurons[0].hotkey == "5HK1"
        assert neurons[0].stake == 2.5
        assert neurons[0].registered_at_block == 100

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.side_effect = (
            TimeoutError()
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_recent_neurons(118)

    async def test_generic_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.side_effect = (
            RuntimeError("boom")
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_recent_neurons(118)


class TestGetLatestBlock:
    async def test_returns_block_info(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_latest_block.return_value = MagicMock(
            number=4242, hash="0xdead", timestamp=1700000000
        )
        async with ChainClient(make_chain_config()) as client:
            block = await client.get_latest_block()
        assert block.number == 4242
        assert block.hash == "0xdead"
        assert block.timestamp == 1700000000

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_latest_block.side_effect = (
            TimeoutError()
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_latest_block()


class TestGetExtrinsic:
    async def test_populates_succeeded_true(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_pylon_module.v1.open_access.get_extrinsic.return_value = (
            make_pylon_extrinsic()
        )
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(7, event_id="ExtrinsicSuccess")]
        )
        async with ChainClient(make_chain_config()) as client:
            ext = await client.get_extrinsic(block_number=100, extrinsic_index=7)
        assert ext.block_number == 100
        assert ext.extrinsic_index == 7
        assert ext.call_module == "Balances"
        assert ext.call_function == "transfer_keep_alive"
        assert ext.call_args == {"value": 1000}
        assert ext.signer_address == "5Signer"
        assert ext.succeeded is True

    async def test_populates_succeeded_false_on_failed_event(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_pylon_module.v1.open_access.get_extrinsic.return_value = (
            make_pylon_extrinsic()
        )
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(7, event_id="ExtrinsicFailed")]
        )
        async with ChainClient(make_chain_config()) as client:
            ext = await client.get_extrinsic(block_number=100, extrinsic_index=7)
        assert ext.succeeded is False

    async def test_not_found_raises_typed_error(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_extrinsic.side_effect = RuntimeError(
            "404 not found"
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_extrinsic(block_number=100, extrinsic_index=7)

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_extrinsic.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_extrinsic(block_number=100, extrinsic_index=7)

    async def test_substrate_failure_leaves_succeeded_none(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_pylon_module.v1.open_access.get_extrinsic.return_value = (
            make_pylon_extrinsic()
        )
        install_substrate_module.query.side_effect = RuntimeError("substrate down")
        async with ChainClient(make_chain_config()) as client:
            ext = await client.get_extrinsic(block_number=100, extrinsic_index=7)
        assert ext.succeeded is None
        assert ext.call_module == "Balances"


class TestPutWeights:
    async def test_calls_pylon_identity(self, install_pylon_module: AsyncMock):
        async with ChainClient(make_chain_config()) as client:
            await client.put_weights({"5HK1": 1.0})
        install_pylon_module.identity.put_weights.assert_awaited_once_with(
            weights={"5HK1": 1.0}
        )

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.identity.put_weights.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.put_weights({"5HK1": 1.0})

    async def test_generic_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.identity.put_weights.side_effect = RuntimeError("boom")
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.put_weights({"5HK1": 1.0})


class TestCheckExtrinsicSuccess:
    async def test_returns_true_on_success_event(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        async with ChainClient(make_chain_config()) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)
        assert ok is True

    async def test_returns_false_on_failed_event(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicFailed")]
        )
        async with ChainClient(make_chain_config()) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)
        assert ok is False

    async def test_index_mismatch_raises_not_found(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(9, event_id="ExtrinsicSuccess")]
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.check_extrinsic_success("0xhash", 3)

    async def test_unrelated_event_then_match(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[
                make_event_record(3, module_id="Balances", event_id="Transfer"),
                make_event_record(3, event_id="ExtrinsicSuccess"),
            ]
        )
        async with ChainClient(make_chain_config()) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)
        assert ok is True

    async def test_timeout_wrapped(
        self,
        install_pylon_module: AsyncMock,
        install_substrate_module: AsyncMock,
    ):
        install_substrate_module.query.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.check_extrinsic_success("0xhash", 3)

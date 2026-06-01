"""Unit tests for ditto.chain.client.ChainClient."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.models import ChainConfig
from ditto.tests.chain.conftest import make_event_record


def make_chain_config(**overrides: Any) -> ChainConfig:
    defaults: dict[str, Any] = {
        "pylon_url": "http://pylon:8080",
        "identity_name": "validator",
        "identity_token": "token",
        "netuid": 118,
    }
    defaults.update(overrides)
    return ChainConfig(**defaults)


def make_pylon_neuron(**overrides: Any) -> MagicMock:
    """Build a Pylon ``Neuron``-shaped object (only the fields we read)."""
    defaults: dict[str, Any] = {
        "hotkey": "5HK1",
        "coldkey": "5CK1",
        "uid": 0,
        "stake": 2.5,
        "axon_info": {"ip": "1.2.3.4"},
        "active": True,
        "validator_permit": True,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def make_neurons_response(neurons: dict[str, MagicMock]) -> MagicMock:
    """Mirror ``GetNeuronsResponse`` (``.block`` + ``.neurons`` dict)."""
    return MagicMock(
        block=MagicMock(number=1, hash="0xblock"),
        neurons=neurons,
    )


def make_pylon_extrinsic_arg(name: str, value: Any) -> MagicMock:
    """Build a Pylon ``ExtrinsicCallArg``-shaped object.

    ``MagicMock(name=...)`` sets the mock's repr name not the ``.name``
    attribute, so we assign after construction.
    """
    arg = MagicMock()
    arg.name = name
    arg.type = "u64"
    arg.value = value
    return arg


def make_pylon_extrinsic(**overrides: Any) -> MagicMock:
    """Build a Pylon ``Extrinsic``-shaped response."""
    call = MagicMock(
        call_module=overrides.pop("call_module", "Balances"),
        call_function=overrides.pop("call_function", "transfer_keep_alive"),
        call_args=overrides.pop(
            "call_args",
            [
                make_pylon_extrinsic_arg("dest", "5Recipient"),
                make_pylon_extrinsic_arg("value", 1000),
            ],
        ),
    )
    defaults: dict[str, Any] = {
        "block_number": overrides.pop("block_number", 100),
        "extrinsic_index": overrides.pop("extrinsic_index", 7),
        "extrinsic_hash": overrides.pop("extrinsic_hash", "0xext"),
        "address": overrides.pop("address", "5Signer"),
    }
    defaults.update(overrides)
    return MagicMock(call=call, **defaults)


class TestChainClientLifecycle:
    """Tests for ChainClient async context manager and AsyncConfig wiring."""

    async def test_aenter_connects(self, install_pylon_module: AsyncMock):
        async with ChainClient(make_chain_config()) as client:
            assert client._pylon is install_pylon_module
        install_pylon_module.__aexit__.assert_awaited()

    async def test_aenter_wraps_failure(self, monkeypatch: pytest.MonkeyPatch):
        artanis = MagicMock()
        artanis.AsyncPylonClient = MagicMock(side_effect=ConnectionError("nope"))
        artanis.AsyncConfig = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        parent = MagicMock()
        parent.artanis = artanis
        monkeypatch.setitem(sys.modules, "pylon_client", parent)
        monkeypatch.setitem(sys.modules, "pylon_client.artanis", artanis)
        with pytest.raises(ChainConnectionError):
            async with ChainClient(make_chain_config()):
                pass

    async def test_methods_require_async_with(self):
        client = ChainClient(make_chain_config())
        with pytest.raises(RuntimeError):
            await client.get_latest_block()

    @pytest.mark.usefixtures("install_pylon_module")
    async def test_aenter_open_access_only_passes_correct_kwargs(self):
        """Open-access-only config must not leak identity kwargs into ``AsyncConfig``.
        Real Pylon validates that identity_name/identity_token come as a pair."""
        import pylon_client.artanis as artanis

        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            open_access_token="open-tok",
        )
        async with ChainClient(config):
            pass
        artanis.AsyncConfig.assert_called_once_with(
            address="http://pylon:8000",
            open_access_token="open-tok",
        )

    @pytest.mark.usefixtures("install_pylon_module")
    async def test_aenter_identity_only_passes_correct_kwargs(self):
        """Identity-only config must not pass an empty ``open_access_token``
        (Pylon treats empty string as a real but invalid token)."""
        import pylon_client.artanis as artanis

        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            identity_name="validator",
            identity_token="id-tok",
        )
        async with ChainClient(config):
            pass
        artanis.AsyncConfig.assert_called_once_with(
            address="http://pylon:8000",
            identity_name="validator",
            identity_token="id-tok",
        )

    @pytest.mark.usefixtures("install_pylon_module")
    async def test_aenter_both_modes_passes_all_kwargs(self):
        """Both auth modes set: ``AsyncConfig`` receives all three tokens.
        Real Pylon supports this for processes that read open-access AND
        also write under an identity."""
        import pylon_client.artanis as artanis

        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            open_access_token="open-tok",
            identity_name="validator",
            identity_token="id-tok",
        )
        async with ChainClient(config):
            pass
        artanis.AsyncConfig.assert_called_once_with(
            address="http://pylon:8000",
            open_access_token="open-tok",
            identity_name="validator",
            identity_token="id-tok",
        )


class TestGetRecentNeurons:
    """Tests for ChainClient.get_recent_neurons."""

    async def test_returns_neuron_info_list(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response({"5HK1": make_pylon_neuron()})
        )
        async with ChainClient(make_chain_config()) as client:
            neurons = await client.get_recent_neurons(118)
        assert len(neurons) == 1
        assert neurons[0].hotkey == "5HK1"
        assert neurons[0].stake == 2.5
        assert neurons[0].is_active is True
        assert neurons[0].validator_permit is True

    async def test_dict_key_overrides_neuron_hotkey(
        self, install_pylon_module: AsyncMock
    ):
        # Pylon sets the dict key authoritative; we mirror that in from_pylon.
        neuron = make_pylon_neuron(hotkey="5INNER")
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response({"5KEY": neuron})
        )
        async with ChainClient(make_chain_config()) as client:
            neurons = await client.get_recent_neurons(118)
        assert neurons[0].hotkey == "5KEY"

    async def test_generic_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.side_effect = (
            RuntimeError("boom")
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_recent_neurons(118)

    async def test_timeout_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.side_effect = (
            TimeoutError()
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_recent_neurons(118)


class TestIsRegistered:
    """Tests for ChainClient.is_registered."""

    async def test_returns_true_when_hotkey_present(
        self, install_pylon_module: AsyncMock
    ):
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response(
                {"5HK1": make_pylon_neuron(), "5HK2": make_pylon_neuron()}
            )
        )
        async with ChainClient(make_chain_config()) as client:
            assert await client.is_registered("5HK1", 118) is True

    async def test_returns_false_when_hotkey_absent(
        self, install_pylon_module: AsyncMock
    ):
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response({"5HK1": make_pylon_neuron()})
        )
        async with ChainClient(make_chain_config()) as client:
            assert await client.is_registered("5UNREGISTERED", 118) is False

    async def test_propagates_chain_error(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_recent_neurons.side_effect = (
            RuntimeError("pylon down")
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.is_registered("5HK1", 118)


class TestGetLatestBlock:
    """Tests for ChainClient.get_latest_block."""

    async def test_returns_block_info(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_latest_block_info.return_value = (
            MagicMock(number=4242, hash="0xdead", timestamp=1700000000)
        )
        async with ChainClient(make_chain_config()) as client:
            block = await client.get_latest_block()
        assert block.number == 4242
        assert block.hash == "0xdead"
        assert block.timestamp == 1700000000

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_latest_block_info.side_effect = (
            TimeoutError()
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_latest_block()


class TestGetExtrinsic:
    """Tests for ChainClient.get_extrinsic."""

    async def test_returns_extrinsic_info(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_extrinsic.return_value = (
            make_pylon_extrinsic()
        )
        async with ChainClient(make_chain_config()) as client:
            ext = await client.get_extrinsic(block_number=100, extrinsic_index=7)
        assert ext.block_number == 100
        assert ext.extrinsic_index == 7
        assert ext.extrinsic_hash == "0xext"
        assert ext.call_module == "Balances"
        assert ext.call_function == "transfer_keep_alive"
        assert ext.call_args == {"dest": "5Recipient", "value": 1000}
        assert ext.signer_address == "5Signer"
        # succeeded is intentionally None: Pylon does not expose block_hash,
        # so the caller must invoke check_extrinsic_success separately.
        assert ext.succeeded is None

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_extrinsic.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_extrinsic(block_number=100, extrinsic_index=7)

    async def test_generic_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.open_access.get_extrinsic.side_effect = RuntimeError(
            "boom"
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_extrinsic(block_number=100, extrinsic_index=7)

    async def test_pylon_not_found_raises_typed(self, install_pylon_module: AsyncMock):
        # Pylon raises a typed PylonNotFound; conftest installs a stand-in class.
        import pylon_client.artanis as artanis

        install_pylon_module.v1.open_access.get_extrinsic.side_effect = (
            artanis.PylonNotFound("not here")
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_extrinsic(block_number=100, extrinsic_index=7)


class TestPutWeights:
    """Tests for ChainClient.put_weights."""

    async def test_calls_pylon_identity(self, install_pylon_module: AsyncMock):
        async with ChainClient(make_chain_config()) as client:
            await client.put_weights({"5HK1": 1.0})
        install_pylon_module.v1.identity.put_weights.assert_awaited_once_with(
            {"5HK1": 1.0}
        )

    async def test_timeout_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.identity.put_weights.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.put_weights({"5HK1": 1.0})

    async def test_generic_error_wrapped(self, install_pylon_module: AsyncMock):
        install_pylon_module.v1.identity.put_weights.side_effect = RuntimeError("boom")
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.put_weights({"5HK1": 1.0})

    @pytest.mark.parametrize("pylon_exc_attr", ["PylonUnauthorized", "PylonForbidden"])
    async def test_pylon_auth_rejection_raises_chain_auth_error(
        self, install_pylon_module: AsyncMock, pylon_exc_attr: str
    ):
        """Pylon returns 401 (bad/missing identity) or 403 (no permit / stake)
        when an identity-mode call is rejected. Both must surface as
        ``ChainAuthError`` so callers can distinguish auth failures from
        transient network issues."""
        import pylon_client.artanis as artanis

        exc_cls = getattr(artanis, pylon_exc_attr)
        install_pylon_module.v1.identity.put_weights.side_effect = exc_cls("denied")
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainAuthError):
                await client.put_weights({"5HK1": 1.0})


@pytest.mark.usefixtures("install_pylon_module")
class TestCheckExtrinsicSuccess:
    """Tests for ChainClient.check_extrinsic_success (the Pylon-events gap)."""

    async def test_returns_true_on_success_event(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        async with ChainClient(make_chain_config()) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)
        assert ok is True

    async def test_returns_false_on_failed_event(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicFailed")]
        )
        async with ChainClient(make_chain_config()) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)
        assert ok is False

    async def test_index_mismatch_raises_not_found(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(9, event_id="ExtrinsicSuccess")]
        )
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.check_extrinsic_success("0xhash", 3)

    async def test_unrelated_event_then_match(
        self, install_substrate_module: AsyncMock
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

    async def test_timeout_wrapped(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.check_extrinsic_success("0xhash", 3)


@pytest.mark.usefixtures("install_pylon_module")
class TestGetColdkeyForHotkey:
    """Tests for ChainClient.get_coldkey_for_hotkey (Pylon-gap substrate read)."""

    async def test_returns_coldkey_string(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.return_value = MagicMock(value="5Coldkey")
        async with ChainClient(make_chain_config()) as client:
            coldkey = await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")
        assert coldkey == "5Coldkey"

    async def test_query_targets_subtensor_owner_storage(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(value="5Coldkey")
        async with ChainClient(make_chain_config()) as client:
            await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")
        install_substrate_module.query.assert_awaited_once()
        kwargs = install_substrate_module.query.await_args.kwargs
        assert kwargs["module"] == "SubtensorModule"
        assert kwargs["storage_function"] == "Owner"
        assert kwargs["params"] == ["5Hotkey"]
        assert kwargs["block_hash"] == "0xblock"

    async def test_unwraps_raw_string_result(self, install_substrate_module: AsyncMock):
        """Some substrate-interface versions return the value directly,
        not wrapped in ``.value``. Verifier must handle both."""
        install_substrate_module.query.return_value = "5Coldkey"
        async with ChainClient(make_chain_config()) as client:
            assert (
                await client.get_coldkey_for_hotkey("5Hotkey", "0xblock") == "5Coldkey"
            )

    async def test_empty_result_raises_not_found(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(value=None)
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")

    async def test_none_result_raises_not_found(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = None
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")

    async def test_timeout_wrapped(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")

    async def test_connection_error_wrapped(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.side_effect = RuntimeError("boom")
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_coldkey_for_hotkey("5Hotkey", "0xblock")


@pytest.mark.usefixtures("install_pylon_module")
class TestGetBlockTimestamp:
    """Tests for ChainClient.get_block_timestamp (Pylon-gap substrate read).

    Substrate ``pallet_timestamp.Now`` is a u64 millisecond unix timestamp;
    the method converts to seconds before returning so downstream code
    never sees the ms representation.
    """

    async def test_returns_seconds_from_milliseconds(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(value=1_700_000_000_456)
        async with ChainClient(make_chain_config()) as client:
            ts = await client.get_block_timestamp("0xblock")
        assert ts == 1_700_000_000

    async def test_unwraps_raw_int_result(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.return_value = 1_700_000_000_000
        async with ChainClient(make_chain_config()) as client:
            assert await client.get_block_timestamp("0xblock") == 1_700_000_000

    async def test_query_targets_timestamp_now_storage(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(value=1_700_000_000_000)
        async with ChainClient(make_chain_config()) as client:
            await client.get_block_timestamp("0xblock")
        kwargs = install_substrate_module.query.await_args.kwargs
        assert kwargs["module"] == "Timestamp"
        assert kwargs["storage_function"] == "Now"
        assert kwargs["block_hash"] == "0xblock"

    async def test_none_result_raises_not_found(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = None
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_block_timestamp("0xblock")

    async def test_timeout_wrapped(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_block_timestamp("0xblock")

    async def test_connection_error_wrapped(self, install_substrate_module: AsyncMock):
        install_substrate_module.query.side_effect = RuntimeError("boom")
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_block_timestamp("0xblock")

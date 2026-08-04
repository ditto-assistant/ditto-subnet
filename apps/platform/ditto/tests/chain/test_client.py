"""Unit tests for ditto.chain.client.ChainClient."""

from __future__ import annotations

import asyncio
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


class AsyncRows:
    """Small async iterator matching ``query_map`` results."""

    def __init__(self, rows: list[tuple[Any, Any]]) -> None:
        self._rows = rows

    def __aiter__(self):
        async def iterate():
            for row in self._rows:
                yield row

        return iterate()


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


class TestGetRegisteredColdkey:
    async def test_returns_owner_for_registered_hotkey(
        self, install_pylon_module: AsyncMock
    ) -> None:
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response({"5HK1": make_pylon_neuron(coldkey="5Coldkey")})
        )
        async with ChainClient(make_chain_config()) as client:
            result = await client.get_registered_coldkey("5HK1", 118)
        assert result == "5Coldkey"

    async def test_returns_none_for_unregistered_hotkey(
        self, install_pylon_module: AsyncMock
    ) -> None:
        install_pylon_module.v1.open_access.get_recent_neurons.return_value = (
            make_neurons_response({"5HK1": make_pylon_neuron()})
        )
        async with ChainClient(make_chain_config()) as client:
            result = await client.get_registered_coldkey("5OTHER", 118)
        assert result is None


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
class TestGetWeights:
    async def test_reads_block_consistent_revealed_matrix(
        self, install_substrate_module: AsyncMock
    ) -> None:
        validator = "5" + "V" * 47
        miner = "5" + "M" * 47
        install_substrate_module.get_chain_head.return_value = "0x" + "ab" * 32
        install_substrate_module.get_block_header.return_value = {
            "header": {"number": 12345}
        }
        install_substrate_module.query_map.side_effect = [
            AsyncRows([(25, [(169, 14745), (0, 65535)])]),
            AsyncRows([(0, "5" + "B" * 47), (25, validator), (169, miner)]),
        ]
        install_substrate_module.query.return_value = "5" + "B" * 47

        async with ChainClient(make_chain_config()) as client:
            snapshot = await client.get_weights(118)

        assert snapshot.netuid == 118
        assert snapshot.block == 12345
        assert snapshot.block_hash == "0x" + "ab" * 32
        assert snapshot.owner_hotkey == "5" + "B" * 47
        assert snapshot.vectors[0].validator_uid == 25
        assert snapshot.vectors[0].validator_hotkey == validator
        assert snapshot.vectors[0].weights[0].uid == 169
        assert snapshot.vectors[0].weights[0].hotkey == miner
        assert snapshot.vectors[0].weights[0].value == 14745
        calls = install_substrate_module.query_map.await_args_list
        assert [call.kwargs["storage_function"] for call in calls] == [
            "Weights",
            "Keys",
        ]
        assert all(call.kwargs["block_hash"] == snapshot.block_hash for call in calls)
        assert install_substrate_module.query.await_args.kwargs["storage_function"] == (
            "SubnetOwnerHotkey"
        )

    async def test_skips_unknown_uids_and_malformed_weights(
        self, install_substrate_module: AsyncMock
    ) -> None:
        validator = "5" + "V" * 47
        install_substrate_module.get_chain_head.return_value = "0x" + "cd" * 32
        install_substrate_module.get_block_header.return_value = {
            "header": {"number": "0x2a"}
        }
        install_substrate_module.query_map.side_effect = [
            AsyncRows([(7, [(999, 4), (7,), (7, 0)])]),
            AsyncRows([(7, validator)]),
        ]
        install_substrate_module.query.return_value = None

        async with ChainClient(make_chain_config()) as client:
            snapshot = await client.get_weights(118)

        assert snapshot.block == 42
        assert snapshot.owner_hotkey is None
        assert snapshot.vectors == ()

    async def test_timeout_is_wrapped(
        self, install_substrate_module: AsyncMock
    ) -> None:
        install_substrate_module.get_chain_head.side_effect = TimeoutError()
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_weights(118)

    async def test_malformed_header_is_wrapped(
        self, install_substrate_module: AsyncMock
    ) -> None:
        install_substrate_module.get_chain_head.return_value = "0x" + "ef" * 32
        install_substrate_module.get_block_header.return_value = {"header": {}}
        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainConnectionError):
                await client.get_weights(118)


@pytest.mark.usefixtures("install_pylon_module")
class TestGetBlockHash:
    async def test_returns_canonical_lowercase_hash(
        self, install_substrate_module: AsyncMock
    ) -> None:
        install_substrate_module.get_block_hash.return_value = "0xABCD"

        async with ChainClient(make_chain_config()) as client:
            block_hash = await client.get_block_hash(123)

        assert block_hash == "0xabcd"
        install_substrate_module.get_block_hash.assert_awaited_once_with(123)

    async def test_missing_hash_raises_not_found(
        self, install_substrate_module: AsyncMock
    ) -> None:
        install_substrate_module.get_block_hash.return_value = None

        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ExtrinsicNotFoundError):
                await client.get_block_hash(123)

    async def test_timeout_is_wrapped(
        self, install_substrate_module: AsyncMock
    ) -> None:
        install_substrate_module.get_block_hash.side_effect = TimeoutError()

        async with ChainClient(make_chain_config()) as client:
            with pytest.raises(ChainTimeoutError):
                await client.get_block_hash(123)


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

    async def test_historical_read_uses_authenticated_archive(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        config = make_chain_config(
            archive_rpc_url="wss://archive.example/rpc",
            archive_rpc_api_key="key with spaces",
        )

        async with ChainClient(config) as client:
            await client.check_extrinsic_success("0xhash", 3)

        substrate_module = sys.modules["async_substrate_interface"]
        substrate_module.AsyncSubstrateInterface.assert_called_once_with(
            url="wss://archive.example/rpc?authorization=key%20with%20spaces"
        )

    async def test_archive_key_is_redacted_from_connection_error(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.side_effect = RuntimeError(
            "failed wss://archive.example?authorization=super-secret"
        )
        config = make_chain_config(
            archive_rpc_url="wss://archive.example",
            archive_rpc_api_key="super-secret",
        )

        async with ChainClient(config) as client:
            with pytest.raises(ChainConnectionError) as exc_info:
                await client.check_extrinsic_success("0xhash", 3)

        assert "super-secret" not in str(exc_info.value)
        assert "<redacted>" in str(exc_info.value)

    async def test_configured_provider_is_tried_before_free_archives(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        config = make_chain_config(
            public_archive_rpc_urls=(
                "wss://archive.chain.opentensor.ai:443",
                "wss://bittensor-finney.api.onfinality.io/public-ws",
            ),
            archive_rpc_url="wss://paid.example/archive",
            archive_rpc_api_key="paid-key",
        )

        async with ChainClient(config) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)

        assert ok is True
        substrate_factory = sys.modules[
            "async_substrate_interface"
        ].AsyncSubstrateInterface
        assert [call.kwargs["url"] for call in substrate_factory.call_args_list] == [
            "wss://paid.example/archive?authorization=paid-key",
        ]

    async def test_configured_provider_failure_falls_through_to_free_archive(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.side_effect = [
            RuntimeError("configured unavailable"),
            MagicMock(value=[make_event_record(3, event_id="ExtrinsicSuccess")]),
        ]
        config = make_chain_config(
            public_archive_rpc_urls=(
                "wss://free-one.example",
                "wss://free-two.example",
            ),
            archive_rpc_url="wss://paid.example/archive",
            archive_rpc_api_key="paid-key",
        )

        async with ChainClient(config) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)

        assert ok is True
        substrate_factory = sys.modules[
            "async_substrate_interface"
        ].AsyncSubstrateInterface
        assert [call.kwargs["url"] for call in substrate_factory.call_args_list] == [
            "wss://paid.example/archive?authorization=paid-key",
            "wss://free-one.example",
        ]

    async def test_configured_free_tier_url_needs_no_key(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        config = make_chain_config(
            public_archive_rpc_urls=("wss://public-fallback.example",),
            archive_rpc_url="wss://provider.example/free-tier",
            archive_rpc_api_key=None,
        )

        async with ChainClient(config) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)

        assert ok is True
        substrate_factory = sys.modules[
            "async_substrate_interface"
        ].AsyncSubstrateInterface
        substrate_factory.assert_called_once_with(
            url="wss://provider.example/free-tier"
        )

    async def test_provider_timeout_falls_through_to_next_archive(
        self, install_substrate_module: AsyncMock
    ):
        async def slow_then_succeed(**_kwargs: Any):
            if install_substrate_module.query.await_count == 1:
                await asyncio.sleep(1)
            return MagicMock(value=[make_event_record(3, event_id="ExtrinsicSuccess")])

        install_substrate_module.query.side_effect = slow_then_succeed
        config = make_chain_config(
            public_archive_rpc_urls=(
                "wss://slow.example",
                "wss://healthy.example",
            ),
            archive_rpc_timeout_seconds=0.01,
        )

        async with ChainClient(config) as client:
            ok = await client.check_extrinsic_success("0xhash", 3)

        assert ok is True
        assert install_substrate_module.query.await_count == 2

    async def test_path_authenticated_provider_is_supported(
        self, install_substrate_module: AsyncMock
    ):
        install_substrate_module.query.return_value = MagicMock(
            value=[make_event_record(3, event_id="ExtrinsicSuccess")]
        )
        config = make_chain_config(
            archive_rpc_url="wss://api-bittensor-mainnet.n.dwellir.com",
            archive_rpc_api_key="key with spaces",
            archive_rpc_auth_mode="path",
        )

        async with ChainClient(config) as client:
            await client.check_extrinsic_success("0xhash", 3)

        substrate_factory = sys.modules[
            "async_substrate_interface"
        ].AsyncSubstrateInterface
        substrate_factory.assert_called_once_with(
            url="wss://api-bittensor-mainnet.n.dwellir.com/key%20with%20spaces"
        )


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

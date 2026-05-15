"""Unit tests for ditto.chain.models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest

from ditto.chain.models import BlockInfo, ChainConfig, ExtrinsicInfo, NeuronInfo


def make_chain_config(**overrides: Any) -> ChainConfig:
    defaults: dict[str, Any] = {
        "pylon_url": "http://pylon:8080",
        "identity_name": "validator",
        "identity_token": "token",
        "netuid": 118,
    }
    defaults.update(overrides)
    return ChainConfig(**defaults)


def make_neuron_info(**overrides: Any) -> NeuronInfo:
    defaults: dict[str, Any] = {
        "hotkey": "5HotkeyAAA",
        "coldkey": "5ColdkeyAAA",
        "uid": 0,
        "stake": 1.0,
    }
    defaults.update(overrides)
    return NeuronInfo(**defaults)


def make_extrinsic_info(**overrides: Any) -> ExtrinsicInfo:
    defaults: dict[str, Any] = {
        "block_number": 100,
        "extrinsic_index": 2,
        "extrinsic_hash": "0xext",
        "call_module": "Balances",
        "call_function": "transfer_keep_alive",
    }
    defaults.update(overrides)
    return ExtrinsicInfo(**defaults)


class TestChainConfig:
    def test_defaults(self):
        config = make_chain_config()
        assert config.subtensor_network == "finney"
        assert config.archive_blocks_cutoff == 300

    def test_frozen(self):
        config = make_chain_config()
        with pytest.raises(FrozenInstanceError):
            config.pylon_url = "http://other"  # type: ignore[misc]

    def test_overrides(self):
        config = make_chain_config(subtensor_network="test", archive_blocks_cutoff=500)
        assert config.subtensor_network == "test"
        assert config.archive_blocks_cutoff == 500


class TestNeuronInfo:
    def test_defaults(self):
        neuron = make_neuron_info()
        assert neuron.axon_info == {}
        assert neuron.is_active is False
        assert neuron.validator_permit is False

    def test_frozen(self):
        neuron = make_neuron_info()
        with pytest.raises(FrozenInstanceError):
            neuron.uid = 99  # type: ignore[misc]

    def test_independent_axon_info_defaults(self):
        a = make_neuron_info()
        b = make_neuron_info()
        assert a.axon_info is not b.axon_info

    def test_from_pylon_uses_dict_key_hotkey(self):
        raw = MagicMock(
            hotkey="5INNER",
            coldkey="5CK",
            uid=4,
            stake=3.0,
            axon_info={"ip": "1.1.1.1"},
            active=True,
            validator_permit=True,
        )
        n = NeuronInfo.from_pylon(raw, hotkey="5KEY")
        assert n.hotkey == "5KEY"
        assert n.coldkey == "5CK"
        assert n.is_active is True
        assert n.validator_permit is True

    def test_from_pylon_falls_back_to_raw_hotkey(self):
        raw = MagicMock(
            hotkey="5RAW",
            coldkey="5CK",
            uid=4,
            stake=3.0,
            axon_info=None,
            active=False,
            validator_permit=False,
        )
        n = NeuronInfo.from_pylon(raw)
        assert n.hotkey == "5RAW"
        assert n.axon_info == {}


class TestExtrinsicInfo:
    def test_succeeded_defaults_none(self):
        ext = make_extrinsic_info()
        assert ext.succeeded is None

    def test_succeeded_set(self):
        ext = make_extrinsic_info(succeeded=True)
        assert ext.succeeded is True

    def test_frozen(self):
        ext = make_extrinsic_info()
        with pytest.raises(FrozenInstanceError):
            ext.call_module = "System"  # type: ignore[misc]

    def test_from_pylon_flattens_call_args(self):
        # MagicMock(name=...) sets repr-name not .name attribute; set after.
        dest = MagicMock()
        dest.name = "dest"
        dest.value = "5Recipient"
        value = MagicMock()
        value.name = "value"
        value.value = 42_000
        raw = MagicMock(
            block_number=42,
            extrinsic_index=3,
            extrinsic_hash="0xhash",
            address="5Signer",
            call=MagicMock(
                call_module="Balances",
                call_function="transfer_keep_alive",
                call_args=[dest, value],
            ),
        )
        ext = ExtrinsicInfo.from_pylon(raw)
        assert ext.block_number == 42
        assert ext.extrinsic_index == 3
        assert ext.call_args == {"dest": "5Recipient", "value": 42_000}
        assert ext.signer_address == "5Signer"
        assert ext.succeeded is None

    def test_from_pylon_accepts_dict_call_args(self):
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(
                call_module="X",
                call_function="y",
                call_args={"a": 1, "b": 2},
            ),
        )
        ext = ExtrinsicInfo.from_pylon(raw)
        assert ext.call_args == {"a": 1, "b": 2}


class TestBlockInfo:
    def test_construction(self):
        block = BlockInfo(number=42, hash="0xabc", timestamp=1700000000)
        assert block.number == 42
        assert block.hash == "0xabc"
        assert block.timestamp == 1700000000

    def test_timestamp_defaults_zero(self):
        block = BlockInfo(number=42, hash="0xabc")
        assert block.timestamp == 0

    def test_frozen(self):
        block = BlockInfo(number=42, hash="0xabc")
        with pytest.raises(FrozenInstanceError):
            block.number = 43  # type: ignore[misc]

    def test_from_pylon(self):
        raw = MagicMock(number=99, hash="0xblock", timestamp=1700000100)
        block = BlockInfo.from_pylon(raw)
        assert block.number == 99
        assert block.hash == "0xblock"
        assert block.timestamp == 1700000100

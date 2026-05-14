"""Unit tests for ditto.chain.models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from ditto.chain.models import BlockInfo, ChainConfig, ExtrinsicInfo, NeuronInfo


def make_chain_config(**overrides: Any) -> ChainConfig:
    defaults: dict[str, Any] = dict(
        pylon_url="http://pylon:8080",
        identity_name="validator",
        identity_token="token",
        netuid=118,
    )
    defaults.update(overrides)
    return ChainConfig(**defaults)


def make_neuron_info(**overrides: Any) -> NeuronInfo:
    defaults: dict[str, Any] = dict(
        hotkey="5HotkeyAAA",
        coldkey="5ColdkeyAAA",
        uid=0,
        stake=1.0,
    )
    defaults.update(overrides)
    return NeuronInfo(**defaults)


def make_extrinsic_info(**overrides: Any) -> ExtrinsicInfo:
    defaults: dict[str, Any] = dict(
        block_number=100,
        extrinsic_index=2,
        call_module="Balances",
        call_function="transfer_keep_alive",
    )
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
        assert neuron.registered_at_block == 0

    def test_frozen(self):
        neuron = make_neuron_info()
        with pytest.raises(FrozenInstanceError):
            neuron.uid = 99  # type: ignore[misc]

    def test_independent_axon_info_defaults(self):
        a = make_neuron_info()
        b = make_neuron_info()
        assert a.axon_info is not b.axon_info


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

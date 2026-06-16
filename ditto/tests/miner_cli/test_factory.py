"""Unit tests for :mod:`ditto.miner_cli.factory`."""

from __future__ import annotations

import argparse

import pytest

from ditto.miner_cli.factory import create_miner_cli_config
from ditto.miner_cli.models import MinerCliConfig


def make_ns(**overrides: object) -> argparse.Namespace:
    """Build an :class:`argparse.Namespace` with sensible defaults for tests."""
    base: dict[str, object] = {"network": "mainnet"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCreateMinerCliConfig:
    def test_mainnet_namespace_yields_mainnet_config(self) -> None:
        config = create_miner_cli_config(make_ns(network="mainnet"))

        assert isinstance(config, MinerCliConfig)
        assert config.network.name == "mainnet"
        assert config.network.subtensor_network == "finney"

    def test_testnet_namespace_yields_testnet_config(self) -> None:
        config = create_miner_cli_config(make_ns(network="testnet"))

        assert config.network.name == "testnet"
        assert config.network.subtensor_network == "test"

    def test_unknown_network_raises(self) -> None:
        with pytest.raises(ValueError):
            create_miner_cli_config(make_ns(network="bogus"))

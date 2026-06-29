"""Unit tests for :mod:`ditto.miner_cli.factory`."""

from __future__ import annotations

import argparse

import pytest

from ditto.miner_cli.factory import create_miner_cli_config
from ditto.miner_cli.models import MinerCliConfig


def make_ns(**overrides: object) -> argparse.Namespace:
    """Build an :class:`argparse.Namespace` with sensible defaults for tests."""
    base: dict[str, object] = {"network": "finney"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCreateMinerCliConfig:
    def test_finney_namespace_yields_mainnet_config(self) -> None:
        config = create_miner_cli_config(make_ns(network="finney"))

        assert isinstance(config, MinerCliConfig)
        assert config.network.name == "finney"
        assert config.network.subtensor_network == "finney"

    def test_test_namespace_yields_testnet_config(self) -> None:
        config = create_miner_cli_config(make_ns(network="test"))

        assert config.network.name == "test"
        assert config.network.subtensor_network == "test"

    def test_unknown_network_raises(self) -> None:
        from ditto.miner_cli.errors import NetworkResolutionError

        with pytest.raises(NetworkResolutionError):
            create_miner_cli_config(make_ns(network="bogus"))


class TestChainEndpointOverride:
    """`args.chain_endpoint` must thread into ``MinerCliConfig.chain_endpoint``
    unchanged when set, and normalise to ``None`` when absent or empty."""

    def test_chain_endpoint_threads_through(self) -> None:
        ns = make_ns(network="local", chain_endpoint="ws://example.org:9944")
        config = create_miner_cli_config(ns)
        assert config.chain_endpoint == "ws://example.org:9944"

    def test_absent_attribute_defaults_to_none(self) -> None:
        # No chain_endpoint on the namespace at all (older callers / tests).
        ns = make_ns(network="local")
        config = create_miner_cli_config(ns)
        assert config.chain_endpoint is None

    def test_explicit_none_stays_none(self) -> None:
        ns = make_ns(network="local", chain_endpoint=None)
        config = create_miner_cli_config(ns)
        assert config.chain_endpoint is None

    def test_empty_string_normalises_to_none(self) -> None:
        """Empty string from an unset env var (`os.environ.get("X")` on
        an unset key returns ``None``, but a present-but-empty env var
        gives ``""``). Normalise to ``None`` so downstream code can
        do a truthiness check without worrying about empty strings."""
        ns = make_ns(network="local", chain_endpoint="")
        config = create_miner_cli_config(ns)
        assert config.chain_endpoint is None

"""Unit tests for :mod:`ditto.miner_cli.network`.

The lookup table is small + locked. These tests pin the invariants
that callers depend on (each canonical name resolves, unknown raises)
rather than restating the table.
"""

from __future__ import annotations

import pytest

from ditto.miner_cli.models import NetworkConfig
from ditto.miner_cli.network import NETWORKS, resolve_network


class TestResolveNetwork:
    @pytest.mark.parametrize("name", ["mainnet", "testnet", "local"])
    def test_each_canonical_name_resolves(self, name: str) -> None:
        result = resolve_network(name)

        assert isinstance(result, NetworkConfig)
        assert result.name == name
        assert result.api_url
        assert result.subtensor_network

    def test_returned_pair_is_the_table_entry(self) -> None:
        """The function returns the same object NETWORKS holds, not a copy."""
        assert resolve_network("mainnet") is NETWORKS["mainnet"]

    def test_unknown_network_raises_value_error(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_network("staging-canary")

        assert "staging-canary" in str(excinfo.value)
        assert "mainnet" in str(excinfo.value)


class TestNetworksTable:
    def test_mainnet_uses_finney_subtensor(self) -> None:
        """Production network must bind to mainnet chain."""
        assert NETWORKS["mainnet"].subtensor_network == "finney"

    def test_testnet_uses_test_subtensor(self) -> None:
        assert NETWORKS["testnet"].subtensor_network == "test"

    def test_local_uses_local_subtensor(self) -> None:
        assert NETWORKS["local"].subtensor_network == "local"

    def test_local_api_url_points_at_docker_compose_stack(self) -> None:
        """The local entry must match the docker-compose API port for
        integration tests + manual smoke."""
        assert NETWORKS["local"].api_url == "http://localhost:8000"

"""Unit tests for :mod:`ditto.miner_cli.network`.

The lookup table is small + locked. These tests pin the invariants
that callers depend on (each canonical name resolves, unknown raises)
rather than restating the table.
"""

from __future__ import annotations

import pytest

from ditto.miner_cli.errors import NetworkResolutionError
from ditto.miner_cli.models import NetworkConfig
from ditto.miner_cli.network import NETWORKS, resolve_network


class TestResolveNetwork:
    @pytest.mark.parametrize("name", ["finney", "test", "local"])
    def test_each_canonical_name_resolves(self, name: str) -> None:
        result = resolve_network(name)

        assert isinstance(result, NetworkConfig)
        assert result.name == name
        assert result.api_url
        assert result.subtensor_network

    def test_returned_pair_is_the_table_entry(self) -> None:
        """The function returns the same object NETWORKS holds, not a copy."""
        assert resolve_network("finney") is NETWORKS["finney"]

    def test_unknown_network_raises_typed_error(self) -> None:
        with pytest.raises(NetworkResolutionError) as excinfo:
            resolve_network("staging-canary")

        assert "staging-canary" in str(excinfo.value)
        assert "finney" in str(excinfo.value)

    def test_colloquial_name_is_not_accepted(self) -> None:
        """``mainnet`` / ``testnet`` are colloquial English; the SDK and
        btcli reject them. Make sure we follow suit so the value flowing
        through the CLI matches what bittensor expects verbatim."""
        with pytest.raises(NetworkResolutionError):
            resolve_network("mainnet")
        with pytest.raises(NetworkResolutionError):
            resolve_network("testnet")


class TestNetworksTable:
    def test_finney_uses_production_platform_api(self) -> None:
        assert NETWORKS["finney"].api_url == "https://platform-api.heyditto.ai/"

    def test_finney_is_mainnet_chain(self) -> None:
        """``finney`` is the bittensor SDK identifier for mainnet. The
        subtensor_network field echoes the dict key so there is no
        translation layer to drift."""
        assert NETWORKS["finney"].subtensor_network == "finney"

    def test_test_is_testnet_chain(self) -> None:
        assert NETWORKS["test"].subtensor_network == "test"

    def test_local_uses_local_subtensor(self) -> None:
        assert NETWORKS["local"].subtensor_network == "local"

    def test_local_api_url_points_at_docker_compose_stack(self) -> None:
        """The local entry must match the docker-compose API port for
        integration tests + manual smoke."""
        assert NETWORKS["local"].api_url == "http://localhost:8000"

"""Locked API URL ↔ subtensor network pairs.

Each Ditto deployment binds an API server to exactly one subtensor
network at boot (see ``ditto.api_server.factory`` lifespan). Letting
the CLI override each side independently is therefore always a miner
footgun: a wrong-network upload either gets rejected at
``/upload/check`` (hotkey not registered on the API's chain) or sends
real TAO to the wrong chain entirely. Exposing one ``--network`` flag
backed by a lookup table is the smallest surface that cannot desync.

If a real decoupled deployment ever appears (Phase 7 canary, staged
rollout), add override flags additively without breaking the existing
flag.
"""

from __future__ import annotations

from ditto.miner_cli.models import NetworkConfig

NETWORKS: dict[str, NetworkConfig] = {
    "mainnet": NetworkConfig(
        name="mainnet",
        api_url="https://api.ditto.subnet.ai",
        subtensor_network="finney",
    ),
    "testnet": NetworkConfig(
        name="testnet",
        api_url="https://staging.api.ditto.subnet.ai",
        subtensor_network="test",
    ),
    "local": NetworkConfig(
        name="local",
        api_url="http://localhost:8000",
        subtensor_network="local",
    ),
}
"""Canonical (API URL, subtensor network) pairs keyed by user-facing name.

The mainnet and testnet URLs are placeholders until the API host is
provisioned (tracked in ``IMPLEMENTATION-PHASES.md`` Phase 7). The
``local`` entry points at the local docker-compose stack used by
integration tests and manual smoke.
"""


def resolve_network(name: str) -> NetworkConfig:
    """Return the :class:`NetworkConfig` for ``name``.

    Args:
        name: One of ``"mainnet"``, ``"testnet"``, ``"local"``.

    Raises:
        ValueError: When ``name`` is not a known network. The argparse
            ``choices=`` argument should normally reject unknown values
            before this is reached; the explicit guard catches direct
            programmatic callers that bypass argparse.
    """
    if name not in NETWORKS:
        known = sorted(NETWORKS)
        raise ValueError(f"unknown network {name!r}; choose from {known}")
    return NETWORKS[name]

"""Factory functions for the chain access layer."""

from __future__ import annotations

from ditto.chain.client import ChainClient
from ditto.chain.models import ChainConfig


def create_chain_client(config: ChainConfig) -> ChainClient:
    """Create a :class:`ChainClient` with sensible defaults.

    Args:
        config: Connection configuration for Pylon plus the subtensor
            network identifier used for event reads.

    Returns:
        A :class:`ChainClient` ready to use as an async context manager.

    Example:
        async with create_chain_client(config) as client:
            block = await client.get_latest_block()
    """
    return ChainClient(config)

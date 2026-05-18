"""Chain access layer wrapping Pylon and async-substrate-interface.

The API server (open-access mode) and validator daemon (identity mode) are
the two consumers. Both go through ``ChainClient`` rather than calling
Pylon or bittensor directly, so the chain library's choice is isolated
from every other module.

``ditto.miner_cli`` is the deliberate exception: it uses the raw bittensor
SDK directly for balance transfers and registrations (operations Pylon
does not expose) and talks to our HTTP API for everything else, so it
never touches ``ChainClient`` at all.

Usage:
    from ditto.chain import ChainConfig, create_chain_client

    # API server (read-only)
    config = ChainConfig(
        pylon_url="http://localhost:8000",
        netuid=118,
        open_access_token="...",
    )
    # Validator daemon (writes weights)
    config = ChainConfig(
        pylon_url="http://localhost:8000",
        netuid=118,
        identity_name="validator",
        identity_token="...",
    )
    async with create_chain_client(config) as client:
        block = await client.get_latest_block()
        extrinsic = await client.get_extrinsic(block.number, 0)
"""

from __future__ import annotations

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.factory import create_chain_client
from ditto.chain.models import (
    BlockInfo,
    ChainConfig,
    ExtrinsicInfo,
    NeuronInfo,
)

__all__ = [
    # Main components
    "ChainClient",
    # Configuration
    "ChainConfig",
    # Result models
    "BlockInfo",
    "ExtrinsicInfo",
    "NeuronInfo",
    # Errors
    "ChainError",
    "ChainAuthError",
    "ChainConnectionError",
    "ChainTimeoutError",
    "ExtrinsicNotFoundError",
    # Factory
    "create_chain_client",
]

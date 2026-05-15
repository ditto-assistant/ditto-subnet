"""Chain access layer wrapping Pylon and async-substrate-interface.

Validator and platform code goes through this module rather than calling
Pylon or bittensor directly. Isolates the chain library's choice from
every consumer.

The miner CLI is a deliberate exception (per the locked architecture): it
uses raw bittensor SDK to submit ``Balances.transfer_keep_alive`` for upload
payment, because Pylon does not expose balance transfers and the CLI is a
short-lived process that does not need a Pylon container.

Usage:
    from ditto.chain import ChainConfig, create_chain_client

    config = ChainConfig(
        pylon_url="http://localhost:8080",
        identity_name="validator",
        identity_token="...",
        netuid=118,
    )
    async with create_chain_client(config) as client:
        block = await client.get_latest_block()
        extrinsic = await client.get_extrinsic(block.number, 0)
        if extrinsic.succeeded:
            ...
"""

from __future__ import annotations

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
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
    "ChainConnectionError",
    "ChainTimeoutError",
    "ExtrinsicNotFoundError",
    # Factory
    "create_chain_client",
]

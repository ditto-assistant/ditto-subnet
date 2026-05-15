"""Smoke test ChainClient against a real running Pylon.

Reads Pylon connection from environment:

    PYLON_URL               default: http://localhost:8000
    PYLON_OPEN_ACCESS_TOKEN required for read-only smoke
    PYLON_IDENTITY_NAME     optional (only for write operations like put_weights)
    PYLON_IDENTITY_TOKEN    paired with PYLON_IDENTITY_NAME
    NETUID                  default: 118
    SUBTENSOR_NETWORK       default: finney

Exercises every read path: get_latest_block, get_recent_neurons,
check_extrinsic_success (which uses async-substrate-interface, not Pylon).
Prints a summary, exits non-zero on any ChainError. Used to validate
ditto.chain against a live backend before downstream modules build on top.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from ditto.chain import ChainConfig, ChainError, create_chain_client

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _config_from_env() -> ChainConfig:
    return ChainConfig(
        pylon_url=os.environ.get("PYLON_URL", "http://localhost:8000"),
        netuid=int(os.environ.get("NETUID", "118")),
        open_access_token=os.environ.get("PYLON_OPEN_ACCESS_TOKEN") or None,
        identity_name=os.environ.get("PYLON_IDENTITY_NAME") or None,
        identity_token=os.environ.get("PYLON_IDENTITY_TOKEN") or None,
        subtensor_network=os.environ.get("SUBTENSOR_NETWORK", "finney"),
    )


async def main() -> int:
    _setup_logging()

    config = _config_from_env()
    logger.info(f"connecting to Pylon at {config.pylon_url} for netuid={config.netuid}")

    try:
        async with create_chain_client(config) as client:
            block = await client.get_latest_block()
            logger.info(
                f"latest block: number={block.number} hash={block.hash[:20]}... "
                f"timestamp={block.timestamp}"
            )

            neurons = await client.get_recent_neurons(config.netuid)
            logger.info(f"netuid={config.netuid}: {len(neurons)} neurons")
            for n in neurons[:5]:
                logger.info(
                    f"  uid={n.uid} hotkey={n.hotkey} stake={n.stake:.4f} "
                    f"active={n.is_active} validator_permit={n.validator_permit}"
                )
            if len(neurons) > 5:
                logger.info(f"  ... and {len(neurons) - 5} more")

            # check_extrinsic_success exercises async-substrate-interface, not
            # Pylon. Index 0 of any block is Timestamp.set, which always succeeds.
            succeeded = await client.check_extrinsic_success(block.hash, 0)
            logger.info(
                f"check_extrinsic_success(block.hash, idx=0) succeeded={succeeded}"
            )
            if not succeeded:
                logger.error("Timestamp.set should always succeed; got False")
                return 1
    except ChainError as e:
        logger.error(f"chain smoke failed: {e}", exc_info=True)
        return 1

    logger.info("smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

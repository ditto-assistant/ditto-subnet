"""Validator worker entrypoint: ``python -m ditto.validator``.

Wires config -> signing key -> HTTP clients -> ChainClient -> the sweep loop,
and drains cleanly on SIGTERM/SIGINT (systemd / pm2 stop). Runs as a singleton
process per validator hotkey — never as part of the API server, and never more
than one instance per hotkey (double weight submission).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import httpx

from ditto.chain import ChainConfig, create_chain_client
from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.dittobench import DittobenchClient
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import load_validator_keypair
from ditto.validator.worker import ValidatorWorker

logger = logging.getLogger(__name__)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is unavailable on non-Unix loops.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _amain() -> int:
    config = parse_validator_config_from_env()
    keypair = load_validator_keypair(config)
    logger.info(
        "validator worker starting hotkey=%s netuid=%d run_size=%s dittobench=%s",
        config.validator_hotkey,
        config.netuid,
        config.run_size,
        config.dittobench_api_url,
    )

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        platform = PlatformClient(config, http)
        dittobench = DittobenchClient(config, http)

        if config.use_sdk_weights:
            # Localnet fallback: weights go through the bittensor SDK, signed by
            # the local hotkey. No Pylon chain client / write identity needed.
            from ditto.validator.sdk_weights import SdkWeightSetter

            logger.info("weight mode: bittensor SDK (set_weights)")
            worker = ValidatorWorker(
                config=config,
                platform=platform,
                dittobench=dittobench,
                chain=None,
                keypair=keypair,
                weight_setter=SdkWeightSetter(config, keypair),
            )
            await worker.run_forever(stop)
        else:
            # Identity mode (write): required for Pylon put_weights.
            chain_config = ChainConfig(
                pylon_url=config.pylon_url,
                netuid=config.netuid,
                identity_name=config.pylon_identity_name,
                identity_token=config.pylon_identity_token,
                subtensor_network=config.subtensor_network,
            )
            logger.info("weight mode: Pylon identity (put_weights)")
            async with create_chain_client(chain_config) as chain:
                worker = ValidatorWorker(
                    config=config,
                    platform=platform,
                    dittobench=dittobench,
                    chain=chain,
                    keypair=keypair,
                )
                await worker.run_forever(stop)
    logger.info("validator worker stopped")
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

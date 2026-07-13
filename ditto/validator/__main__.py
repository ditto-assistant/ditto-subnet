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
import os
import signal

import httpx

from ditto.chain import ChainConfig, create_chain_client
from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.dittobench import DittobenchClient
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import load_validator_keypair
from ditto.validator.telemetry import (
    build_telemetry,
    parse_telemetry_config_from_env,
)
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

    # Optional public telemetry (wandb). Off by default; a disabled instance is
    # a cheap no-op. Built once and shared by whichever weight mode runs.
    telemetry = build_telemetry(
        parse_telemetry_config_from_env(),
        validator_hotkey=config.validator_hotkey,
        netuid=config.netuid,
    )

    try:
        async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
            platform = PlatformClient(config, http)
            dittobench = DittobenchClient(config, http)

            # Every validator both scores and sets weights, so it always runs a
            # Pylon identity client. One token authorizes both the put_weights
            # write and the open-access permit self-check.
            chain_config = ChainConfig(
                pylon_url=config.pylon_url,
                netuid=config.netuid,
                identity_name=config.pylon_identity_name,
                identity_token=config.pylon_token,
                open_access_token=config.pylon_token,
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
                    telemetry=telemetry,
                )
                _apply_ditto_logging()  # re-assert: bittensor has initialised
                await worker.run_forever(stop)
    finally:
        telemetry.close()
    logger.info("validator worker stopped")
    return 0


def _apply_ditto_logging() -> None:
    """Make the worker's own log lines visible, and keep them visible.

    bittensor takes over Python logging when it initialises (lazily, on first
    chain/SDK use): it clamps the level of *every logger that already exists* —
    including ``ditto.validator.worker`` and friends — to WARNING, which silently
    swallows the INFO lines we rely on (queue sweeps, per-agent scores, weight
    submissions). Setting only the parent ``ditto`` level does not help, because
    a child's own WARNING level filters the record before it can propagate up.

    So: give the ``ditto`` tree its own handler + level (overridable via
    ``VALIDATOR_LOG_LEVEL``, default INFO) with propagation off, and reset every
    existing ``ditto.*`` child to NOTSET so it inherits that level again. This is
    idempotent and must be called **again after bittensor has initialised** (see
    the calls guarding ``run_forever``) to undo bittensor's clamp.
    """
    level_name = os.environ.get("VALIDATOR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    fmt = logging.Formatter(log_format)
    logging.basicConfig(level=level, format=log_format)
    ditto_logger = logging.getLogger("ditto")
    ditto_logger.setLevel(level)
    ditto_logger.propagate = False
    if not any(getattr(h, "_ditto_handler", False) for h in ditto_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        handler._ditto_handler = True  # type: ignore[attr-defined]
        ditto_logger.addHandler(handler)
    # Undo any per-child level clamp (e.g. bittensor's) so children inherit
    # ``ditto`` (INFO) rather than a stale WARNING set behind our back.
    for name, child in logging.Logger.manager.loggerDict.items():
        if name.startswith("ditto.") and isinstance(child, logging.Logger):
            child.setLevel(logging.NOTSET)
            child.disabled = False


def main() -> None:
    _apply_ditto_logging()
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

"""Screener worker entrypoint: ``python -m ditto.screener``.

Wires config -> signing key -> HTTP client + build gate -> the sweep loop, and
drains cleanly on SIGTERM/SIGINT (systemd / pm2 stop). Runs as a singleton
process per screener hotkey.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import httpx

from ditto.screener.config import parse_screener_config_from_env
from ditto.screener.gate import BuildGate
from ditto.screener.platform import PlatformClient
from ditto.screener.signing import load_screener_keypair
from ditto.screener.worker import ScreenerWorker

logger = logging.getLogger(__name__)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _amain() -> int:
    config = parse_screener_config_from_env()
    keypair = load_screener_keypair(config)
    logger.info(
        "screener worker starting hotkey=%s netuid=%d platform=%s",
        config.screener_hotkey,
        config.netuid,
        config.platform_api_url,
    )

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        platform = PlatformClient(config, http)
        gate = BuildGate(config, http)
        worker = ScreenerWorker(
            config=config, platform=platform, gate=gate, keypair=keypair
        )
        _apply_ditto_logging()  # re-assert after bittensor init (see validator)
        await worker.run_forever(stop)
    logger.info("screener worker stopped")
    return 0


def _apply_ditto_logging() -> None:
    """Give the ``ditto`` logger tree its own INFO handler and undo any clamp.

    bittensor clamps existing loggers to WARNING when it initialises; mirror the
    validator's fix so the screener's INFO lines (sweeps, per-agent verdicts)
    stay visible. Overridable via ``SCREENER_LOG_LEVEL``. Idempotent.
    """
    level_name = os.environ.get("SCREENER_LOG_LEVEL", "INFO").upper()
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
    for name, child in logging.Logger.manager.loggerDict.items():
        if name.startswith("ditto.") and isinstance(child, logging.Logger):
            child.setLevel(logging.NOTSET)
            child.disabled = False


def main() -> None:
    _apply_ditto_logging()
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

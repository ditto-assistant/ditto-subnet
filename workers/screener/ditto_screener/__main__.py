"""Screener worker entrypoint: ``python -m ditto_screener``.

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

from ditto_screener.config import parse_screener_config_from_env
from ditto_screener.gate import BuildGate
from ditto_screener.heartbeat import SystemMetricsCollector
from ditto_screener.l2_review import L2_HARNESS_REVISION, L2_PROMPT_REVISION
from ditto_screener.platform import PlatformClient
from ditto_screener.policy import ReviewJournal, load_policy_engine
from ditto_screener.readiness import ReadinessServer
from ditto_screener.signing import load_screener_keypair
from ditto_screener.worker import ScreenerWorker

logger = logging.getLogger(__name__)

# The screener package (``ditto_screener``), whose logger tree bittensor clamps
# to WARNING on init and which ``_apply_ditto_logging`` must un-clamp. Resolve
# from ``__package__`` rather than ``__name__``: under ``python -m ditto_screener``
# (the documented entrypoint) ``__name__`` is ``"__main__"``, whereas
# ``__package__`` stays ``"ditto_screener"`` in both that and the console-script
# import path. The literal is only a last-resort fallback for direct-file exec.
_PACKAGE_ROOT = (__package__ or "ditto_screener").split(".")[0]


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _amain() -> int:
    config = parse_screener_config_from_env()
    keypair = load_screener_keypair(config)
    # load_screener_keypair imports bittensor, which clamps our loggers to
    # WARNING; re-assert immediately so the startup lines below are not lost.
    _apply_ditto_logging()
    logger.info(
        "screener worker starting hotkey=%s netuid=%d platform=%s",
        config.screener_hotkey,
        config.netuid,
        config.platform_api_url,
    )

    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)

    # Optional readiness server for MIG autohealing (SCREENER_READINESS_PORT set
    # by the fleet bootstrap; unset/0 on the pet VM disables it). Reports
    # "starting" until the sweep loop is entered, then "ready".
    readiness = _make_readiness_server()
    if readiness is not None:
        readiness.start()

    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        platform = PlatformClient(config, http)
        policy = load_policy_engine(
            config.policy_manifest_file, l2_mode=config.l2_review_mode
        )
        journal = ReviewJournal(config.review_journal_file)
        logger.info(
            "screening policy loaded version=%d rotation=%s manifest_digest=%s",
            policy.manifest.policy_version,
            policy.manifest.rotation_id,
            policy.manifest.digest,
        )
        logger.info(
            "source review L2 mode=%s model=%s fallbacks=%s L3_model=%s "
            "L3_provider=%s prompt=%s harness=%s "
            "steps=%d input_tokens=%d output_tokens=%d cost_usd=%.2f timeout_s=%.0f "
            "analyst_reasoning=%s critic_reasoning=%s",
            config.l2_review_mode,
            config.l2_review_model,
            ",".join(config.l2_fallback_models),
            config.l3_review_model,
            config.l3_review_provider,
            L2_PROMPT_REVISION,
            L2_HARNESS_REVISION,
            config.l2_max_steps,
            config.l2_max_input_tokens,
            config.l2_max_output_tokens,
            config.l2_max_cost_usd,
            config.l2_timeout_seconds,
            config.l2_analyst_reasoning_effort,
            config.l2_critic_reasoning_effort,
        )
        gate = BuildGate(config, http, policy=policy, journal=journal)
        worker = ScreenerWorker(
            config=config,
            platform=platform,
            gate=gate,
            keypair=keypair,
            system_metrics=SystemMetricsCollector(),
        )
        _apply_ditto_logging()  # re-assert after bittensor init (see validator)
        if readiness is not None:
            readiness.set_ready()
        try:
            await worker.run_forever(stop)
        finally:
            if readiness is not None:
                readiness.stop()
    logger.info("screener worker stopped")
    return 0


def _make_readiness_server() -> ReadinessServer | None:
    """Build the readiness server from SCREENER_READINESS_PORT, or None if unset.

    Kept out of the config dataclass on purpose: it is an entrypoint/operational
    concern, and a bad value must never block the worker from starting.
    """
    raw = os.environ.get("SCREENER_READINESS_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("ignoring non-integer SCREENER_READINESS_PORT=%r", raw)
        return None
    if not 1 <= port <= 65535:
        # Out-of-range (incl. >65535, which would raise OverflowError inside
        # ReadinessServer and bypass the OSError handler below).
        logger.warning("ignoring out-of-range SCREENER_READINESS_PORT=%d", port)
        return None
    try:
        return ReadinessServer(port)
    except OSError as e:
        logger.warning("could not bind readiness port %d: %s", port, e)
        return None


def _apply_ditto_logging() -> None:
    """Give the screener's logger tree its own INFO handler and undo any clamp.

    bittensor clamps every existing logger to WARNING when it initialises (which
    happens lazily during ``load_screener_keypair``); mirror the validator's fix
    so the screener's INFO lines (sweeps, per-agent verdicts) stay visible. The
    clamp lands on this package's tree — ``ditto_screener.*`` — so that is the
    tree we must re-assert; targeting a bare ``ditto`` tree (the validator's
    package name) silently no-ops here and leaves INFO suppressed. Overridable
    via ``SCREENER_LOG_LEVEL``. Idempotent.
    """
    level_name = os.environ.get("SCREENER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    fmt = logging.Formatter(log_format)
    logging.basicConfig(level=level, format=log_format)
    package_logger = logging.getLogger(_PACKAGE_ROOT)
    package_logger.setLevel(level)
    package_logger.propagate = False
    if not any(getattr(h, "_ditto_handler", False) for h in package_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        handler._ditto_handler = True  # type: ignore[attr-defined]
        package_logger.addHandler(handler)
    child_prefix = f"{_PACKAGE_ROOT}."
    for name, child in logging.Logger.manager.loggerDict.items():
        if name.startswith(child_prefix) and isinstance(child, logging.Logger):
            child.setLevel(logging.NOTSET)
            child.disabled = False


def main() -> None:
    _apply_ditto_logging()
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

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
from typing import Protocol

import httpx

from ditto.api_models.coding import CodingCapabilityCertificationReceipt
from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseResponse,
)
from ditto.chain import ChainConfig, create_chain_client
from ditto.system_health import SystemMetricsCollector
from ditto.validator.coding_canary import CodingCanaryWorker
from ditto.validator.coding_canary_runtime import CodingCanaryRuntime
from ditto.validator.coding_evidence_uploader import CodingSealedEvidenceUploader
from ditto.validator.coding_publication import CodingPublicationClient
from ditto.validator.coding_supervisor import CodingSupervisorRuntime
from ditto.validator.coding_worker import CodingShadowWorker
from ditto.validator.config import parse_validator_config_from_env
from ditto.validator.dittobench import DittobenchClient
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import load_validator_keypair, sign_coding_certification
from ditto.validator.stack_health import StackHealthCollector
from ditto.validator.telemetry import (
    build_telemetry,
    parse_telemetry_config_from_env,
)
from ditto.validator.update_control import (
    bootstrap_should_start_drained,
    mark_bootstrap_resumed,
    write_update_state,
)
from ditto.validator.worker import ValidatorWorker

logger = logging.getLogger(__name__)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
    drain_requested: asyncio.Event,
    *,
    persist_bootstrap_resume: bool,
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is unavailable on non-Unix loops.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    # The repo-owned updater uses USR1/USR2 only after verifying the running
    # image's update-protocol label. USR1 stops new work; USR2 cancels a timed-
    # out drain without restarting or losing the active lease.
    with contextlib.suppress(NotImplementedError, AttributeError):
        loop.add_signal_handler(signal.SIGUSR1, drain_requested.set)

        def resume() -> None:
            if persist_bootstrap_resume and not mark_bootstrap_resumed():
                return
            drain_requested.clear()

        loop.add_signal_handler(signal.SIGUSR2, resume)


async def _amain() -> int:
    stop = asyncio.Event()
    drain_requested = asyncio.Event()
    bootstrap_enabled = os.environ.get(
        "VALIDATOR_START_DRAINED", "false"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    bootstrap_drain_pending = bootstrap_should_start_drained(bootstrap_enabled)
    if bootstrap_drain_pending:
        drain_requested.set()
    _install_signal_handlers(
        asyncio.get_running_loop(),
        stop,
        drain_requested,
        persist_bootstrap_resume=bootstrap_enabled,
    )
    # Install USR1/USR2 before publishing any updater-visible state. Otherwise
    # a check racing slow config or wallet loading could hit Unix's default
    # SIGUSR1 action and terminate PID 1.
    write_update_state("starting")
    config = parse_validator_config_from_env()
    keypair = load_validator_keypair(config)
    logger.info(
        "validator worker starting hotkey=%s netuid=%d run_size=%s dittobench=%s",
        config.validator_hotkey,
        config.netuid,
        config.run_size,
        config.dittobench_api_url,
    )

    # Optional public telemetry (wandb). Off by default; a disabled instance is
    # a cheap no-op. Built once and shared by whichever weight mode runs.
    telemetry = build_telemetry(
        parse_telemetry_config_from_env(),
        validator_hotkey=config.validator_hotkey,
        netuid=config.netuid,
    )

    try:
        async with (
            httpx.AsyncClient(timeout=config.http_timeout_seconds) as http,
            contextlib.AsyncExitStack() as coding_clients,
        ):
            platform = PlatformClient(config, http, keypair)
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
                coding_canary: CodingCanaryWorker | None = None
                if config.coding_canary_enabled:

                    def _sign_canary_receipt(
                        lease: CodingCertificationLeaseResponse,
                        receipt: CodingCapabilityCertificationReceipt,
                    ) -> str:
                        return sign_coding_certification(
                            keypair,
                            validator_hotkey=config.validator_hotkey,
                            agent_id=lease.authority.agent_id,
                            bench_version=lease.authority.bench_version,
                            lease_id=lease.authority.lease_id,
                            screened_image_sha256=lease.authority.screened_image_sha256,
                            receipt=receipt,
                        )

                    coding_canary = CodingCanaryWorker(
                        platform=platform,
                        runtime=CodingCanaryRuntime(config, http),
                        sign_receipt=_sign_canary_receipt,
                        poll_seconds=config.coding_canary_poll_seconds,
                    )
                    logger.info("coding canary worker enabled")
                worker = ValidatorWorker(
                    config=config,
                    platform=platform,
                    dittobench=dittobench,
                    chain=chain,
                    keypair=keypair,
                    telemetry=telemetry,
                    system_metrics=SystemMetricsCollector(),
                    stack_health=StackHealthCollector(config, http),
                    after_score=(
                        coding_canary.offer if coding_canary is not None else None
                    ),
                )
                coding_worker: CodingShadowWorker | None = None
                if config.coding_shadow_enabled:
                    coding_local_http = await coding_clients.enter_async_context(
                        httpx.AsyncClient(
                            timeout=config.http_timeout_seconds,
                            trust_env=False,
                        )
                    )
                    coding_storage_http = await coding_clients.enter_async_context(
                        httpx.AsyncClient(
                            timeout=httpx.Timeout(
                                300.0,
                                connect=10.0,
                                pool=10.0,
                            ),
                            trust_env=False,
                        )
                    )
                    publication = CodingPublicationClient(
                        base_url=config.dittobench_api_url,
                        control_token=config.dittobench_control_token,
                        client=coding_local_http,
                    )
                    uploader = CodingSealedEvidenceUploader(
                        platform=platform,
                        outbox=publication,
                        storage_client=coding_storage_http,
                    )
                    coding_runtime = CodingSupervisorRuntime(config, http, platform)
                    coding_worker = CodingShadowWorker(
                        platform=platform,
                        runtime=coding_runtime,
                        publication=publication,
                        uploader=uploader,
                        instance_id=config.coding_shadow_instance_id,
                        poll_seconds=config.coding_shadow_poll_seconds,
                    )
                    logger.info(
                        "shadow coding worker enabled instance=%s",
                        config.coding_shadow_instance_id,
                    )
                _apply_ditto_logging()  # re-assert: bittensor has initialised

                async def run_ordinary_worker() -> None:
                    await worker.run_forever(
                        stop,
                        drain_requested=drain_requested,
                        bootstrap_resume=(
                            mark_bootstrap_resumed if bootstrap_drain_pending else None
                        ),
                        extra_busy=_extra_busy(coding_worker, coding_canary),
                    )

                extras: list[tuple[str, _ExtraWorker]] = []
                if coding_worker is not None:
                    extras.append(("validator-coding-shadow-worker", coding_worker))
                if coding_canary is not None:
                    extras.append(("validator-coding-canary-worker", coding_canary))
                try:
                    if not extras:
                        await run_ordinary_worker()
                    else:
                        async with asyncio.TaskGroup() as group:
                            group.create_task(
                                run_ordinary_worker(),
                                name="validator-ordinary-worker",
                            )
                            for name, extra_worker in extras:
                                group.create_task(
                                    extra_worker.run_forever(
                                        stop,
                                        drain_requested=drain_requested,
                                    ),
                                    name=name,
                                )
                finally:
                    stop.set()
    finally:
        write_update_state("stopping")
        telemetry.close()
    logger.info("validator worker stopped")
    return 0


class _ExtraWorker(Protocol):
    busy: bool

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event,
    ) -> None: ...


def _extra_busy(
    coding_worker: CodingShadowWorker | None,
    coding_canary: CodingCanaryWorker | None,
):
    if coding_worker is None and coding_canary is None:
        return None

    def _busy() -> bool:
        return (coding_worker is not None and coding_worker.busy) or (
            coding_canary is not None and coding_canary.busy
        )

    return _busy


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

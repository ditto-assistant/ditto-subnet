"""Explicit, default-off independent V13 public replay worker entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

import httpx

from ditto_screener import __version__
from ditto_screener.config import ScreenerConfig, parse_screener_config_from_env
from ditto_screener.enrollment import ensure_node_credentials_from_env
from ditto_screener.errors import PlatformError
from ditto_screener.gate import BuildGate
from ditto_screener.heartbeat import (
    ReviewSettingsStatus,
    ScreenerHeartbeatRequest,
    collect_fleet_release,
    collect_host_specs,
)
from ditto_screener.platform import PlatformClient
from ditto_screener.policy import (
    ReviewJournal,
    builtin_policy_manifest,
    load_policy_engine,
)
from ditto_screener.replay_api import ReplayApiClient
from ditto_screener.replay_process import ReplayProcessIdentity
from ditto_screener.replay_public import ReplayPublicRunner
from ditto_screener.review_settings import bootstrap_review_settings
from ditto_screener.signing import load_screener_keypair, sign_heartbeat
from ditto_screening_protocol import SCREENING_POLICY_VERSION

logger = logging.getLogger(__name__)
_NODE = "subnet-screener-2"
_INSTANCE = f"{_NODE}-worker-1"


class ReplayHeartbeatPublisher:
    """Emit the ordinary hotkey heartbeat with an independent process proof."""

    def __init__(
        self, *, config: ScreenerConfig, keypair: Any, api: ReplayApiClient
    ) -> None:
        self._config = config
        self._keypair = keypair
        self._api = api
        self._last_timestamp = 0
        self._host_specs = collect_host_specs()
        if self._host_specs is None:
            raise ValueError("v13 replay requires attested host specs")
        self._release = collect_fleet_release(
            builtin_policy_version=SCREENING_POLICY_VERSION
        )
        if self._release.version is None:
            raise ValueError("v13 replay requires an attested release version")
        bootstrap = bootstrap_review_settings(config)
        manifest = builtin_policy_manifest(
            bootstrap.settings.policy_manifest_profile,
            bootstrap.settings.policy_manifest_rotation_id,
        )
        self._review_settings = ReviewSettingsStatus(
            revision=bootstrap.revision,
            scope=bootstrap.scope,
            mode=bootstrap.settings.mode,
            checksum=bootstrap.checksum,
            source="bootstrap",
            policy_manifest_profile=bootstrap.settings.policy_manifest_profile,
            policy_manifest_rotation_id=bootstrap.settings.policy_manifest_rotation_id,
            policy_manifest_digest=manifest.digest,
        )

    async def publish(self) -> None:
        timestamp = max(int(time.time()), self._last_timestamp + 1)
        self._last_timestamp = timestamp
        fields: dict[str, Any] = {
            "screener_hotkey": self._config.screener_hotkey,
            "software_version": __version__,
            "protocol_version": 7,
            "policy_version": 13,
            "state": "polling",
            "active_agent_id": None,
            "instance_id": _INSTANCE,
            "progress": None,
            "system_metrics": None,
            "review_settings": self._review_settings,
            "host_specs": self._host_specs,
            "release": self._release,
            "timestamp": timestamp,
        }
        signature = sign_heartbeat(self._keypair, **fields)
        response = await self._api.heartbeat(
            ScreenerHeartbeatRequest(**fields, signature=signature)
        )
        if not response.accepted:
            raise PlatformError("v13 replay heartbeat was not accepted")


async def _heartbeat_loop(
    publisher: ReplayHeartbeatPublisher, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=120.0)
            return
        except TimeoutError:
            pass
        try:
            await publisher.publish()
        except Exception:  # noqa: BLE001 - admission still checks fresh heartbeat
            logger.warning("v13 replay process heartbeat unavailable")


async def _amain() -> int:
    if os.environ.get("SCREENER_REPLAY_WORKER_ENABLED") != "1":
        raise ValueError("v13 replay worker is disabled")
    key_file = os.environ.get("SCREENER_REPLAY_PROCESS_KEY_FILE")
    if not key_file:
        raise ValueError("v13 replay process key file is unset")
    await ensure_node_credentials_from_env()
    config = parse_screener_config_from_env()
    if (
        config.node_id != _NODE
        or config.instance_id != _INSTANCE
        or config.v13_runtime_receipts_mode != "shadow"
        or not config.require_rootless_docker
    ):
        raise ValueError("v13 replay worker isolation settings are incomplete")
    identity = ReplayProcessIdentity(
        node_id=_NODE, instance_id=_INSTANCE, key_file=Path(key_file)
    )
    keypair = load_screener_keypair(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        platform = PlatformClient(config, http, keypair=keypair)
        api = ReplayApiClient(platform, http, identity)
        heartbeat = ReplayHeartbeatPublisher(config=config, keypair=keypair, api=api)
        policy = load_policy_engine(
            config.policy_manifest_file, l2_mode=config.l2_review_mode
        )
        gate = BuildGate(
            config,
            http,
            policy=policy,
            journal=ReviewJournal(config.review_journal_file),
        )
        runner = ReplayPublicRunner(api=api, gate=gate, http=http)
        await heartbeat.publish()
        heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat, stop))
        try:
            while not stop.is_set():
                try:
                    replay_id = await runner.run_once()
                except Exception:  # noqa: BLE001 - a lost claim response must not retry
                    logger.warning("v13 replay claim unavailable")
                    replay_id = None
                if replay_id is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=30.0)
        finally:
            stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()

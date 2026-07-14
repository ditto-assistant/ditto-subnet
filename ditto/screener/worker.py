"""The screener sweep loop.

One sweep: lease one eligible agent from the platform, screen it through the
build gate, and post a lease-bound signed verdict. Agents are processed one at
a time because builds are heavy and serial execution keeps host load predictable.

A single bad submission or a transient platform error must never stall the loop:
each agent is guarded, and a failed platform call is logged and retried next
sweep. The loop drains promptly when the queue is non-empty and sleeps
``poll_seconds`` when it is idle, exiting cleanly when ``stop`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from ditto import __version__
from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    ScreenerHeartbeatRequest,
    ScreenerQueueItem,
    ScreenerRuntimeState,
)
from ditto.screener.errors import PlatformError
from ditto.screener.signing import sign_heartbeat, sign_verdict

if TYPE_CHECKING:
    from uuid import UUID

    from ditto.screener.config import ScreenerConfig
    from ditto.screener.gate import BuildGate
    from ditto.screener.platform import PlatformClient
    from ditto.system_health import SystemMetricsCollector

logger = logging.getLogger(__name__)

_HEARTBEAT_PROTOCOL_VERSION = 1
_HEARTBEAT_MIN_INTERVAL_SECONDS = 120.0
_ACTIVE_HEARTBEAT_SECONDS = 120.0


class ScreenerWorker:
    """Drains the screener queue, gating each agent and posting a verdict."""

    def __init__(
        self,
        *,
        config: ScreenerConfig,
        platform: PlatformClient,
        gate: BuildGate,
        keypair: Any,
        system_metrics: SystemMetricsCollector | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._gate = gate
        self._keypair = keypair
        self._system_metrics = system_metrics
        self._active_agent_id: UUID | None = None
        self._last_heartbeat_timestamp = 0
        self._last_heartbeat_monotonic = float("-inf")
        self._last_heartbeat_state: ScreenerRuntimeState | None = None

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep until ``stop`` is set, sleeping when the queue is empty."""
        logger.info(
            "screener worker started hotkey=%s netuid=%d platform=%s",
            self._config.screener_hotkey,
            self._config.netuid,
            self._config.platform_api_url,
        )
        while not stop.is_set():
            await self._report_heartbeat("polling")
            try:
                processed = await self._sweep(stop)
            except PlatformError as e:
                logger.warning("sweep failed (retrying next cycle): %s", e)
                processed = 0
            if processed == 0 and not stop.is_set():
                await self._sleep_or_stop(stop, self._config.poll_seconds)
        logger.info("screener worker stopped")

    async def _report_heartbeat(
        self, state: ScreenerRuntimeState, *, force: bool = False
    ) -> None:
        """Best-effort dedicated screener report; never gate screening work."""
        now_monotonic = time.monotonic()
        if (
            not force
            and state == self._last_heartbeat_state
            and now_monotonic - self._last_heartbeat_monotonic
            < _HEARTBEAT_MIN_INTERVAL_SECONDS
        ):
            return
        try:
            timestamp = max(int(time.time()), self._last_heartbeat_timestamp + 1)
            system_metrics = (
                self._system_metrics.collect()
                if self._system_metrics is not None
                else None
            )
            signature = sign_heartbeat(
                self._keypair,
                screener_hotkey=self._config.screener_hotkey,
                software_version=__version__,
                protocol_version=_HEARTBEAT_PROTOCOL_VERSION,
                policy_version=SCREENING_POLICY_VERSION,
                state=state,
                active_agent_id=self._active_agent_id,
                system_metrics=system_metrics,
                timestamp=timestamp,
            )
            request = ScreenerHeartbeatRequest(
                screener_hotkey=self._config.screener_hotkey,
                software_version=__version__,
                protocol_version=_HEARTBEAT_PROTOCOL_VERSION,
                policy_version=SCREENING_POLICY_VERSION,
                state=state,
                active_agent_id=self._active_agent_id,
                system_metrics=system_metrics,
                timestamp=timestamp,
                signature=signature,
            )
            await self._platform.submit_heartbeat(request)
            self._last_heartbeat_timestamp = timestamp
            self._last_heartbeat_monotonic = now_monotonic
            self._last_heartbeat_state = state
        except Exception as e:  # noqa: BLE001 - observability must never gate work
            logger.warning("screener heartbeat failed (screening continues): %s", e)

    async def _heartbeat_while_active(self, stop: asyncio.Event) -> None:
        """Refresh screening state throughout a long build/canary run."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_ACTIVE_HEARTBEAT_SECONDS)
            except TimeoutError:
                await self._report_heartbeat("screening", force=True)

    async def _sweep(self, stop: asyncio.Event) -> int:
        """Lease and screen the next eligible agent; return how many were done."""
        required_policy = await self._platform.get_required_policy_version()
        if required_policy != SCREENING_POLICY_VERSION:
            raise PlatformError(
                "screening policy mismatch before claim: platform requires "
                f"{required_policy}, worker supports {SCREENING_POLICY_VERSION}"
            )
        queue = await self._platform.claim_next()
        if queue.required_policy_version != SCREENING_POLICY_VERSION:
            raise PlatformError(
                "platform requires screening policy "
                f"{queue.required_policy_version}, worker supports "
                f"{SCREENING_POLICY_VERSION}"
            )
        if not queue.items:
            return 0
        logger.info("screener sweep: %d agent(s) to screen", len(queue.items))
        done = 0
        for item in queue.items:
            if stop.is_set():
                break
            await self._screen_one(item)
            done += 1
        return done

    async def _screen_one(self, item: ScreenerQueueItem) -> None:
        """Gate one agent and post its signed verdict. Never raises."""
        agent_id = item.agent_id
        if item.attempt_id is None:
            logger.error("claimed agent_id=%s without a screening attempt id", agent_id)
            return
        self._active_agent_id = agent_id
        await self._report_heartbeat("screening", force=True)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_while_active(heartbeat_stop)
        )
        try:
            artifact = await self._platform.get_artifact(agent_id)
            result = await self._gate.screen(
                agent_id=agent_id,
                sha256=item.sha256,
                download_url=str(artifact.download_url),
            )
            signature = sign_verdict(
                self._keypair,
                screener_hotkey=self._config.screener_hotkey,
                agent_id=agent_id,
                passed=result.passed,
                policy_version=SCREENING_POLICY_VERSION,
                attempt_id=item.attempt_id,
            )
            resp = await self._platform.submit_result(
                agent_id,
                signature=signature,
                passed=result.passed,
                policy_version=SCREENING_POLICY_VERSION,
                detail=result.detail,
                attempt_id=item.attempt_id,
            )
            logger.info(
                "screened agent_id=%s miner=%s passed=%s -> %s%s",
                agent_id,
                item.miner_hotkey,
                result.passed,
                resp.status,
                f" detail={result.detail!r}" if result.detail else "",
            )
        except PlatformError as e:
            # A late/conflicting verdict (409) or transient error: log + move on.
            logger.warning("verdict for agent_id=%s not applied: %s", agent_id, e)
        finally:
            heartbeat_stop.set()
            await heartbeat_task
            self._active_agent_id = None
            await self._report_heartbeat("polling", force=True)

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early if ``stop`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

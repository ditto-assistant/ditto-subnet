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
from typing import TYPE_CHECKING, Any

from ditto.api_models.screener import SCREENING_POLICY_VERSION, ScreenerQueueItem
from ditto.screener.errors import PlatformError
from ditto.screener.signing import sign_verdict

if TYPE_CHECKING:
    from ditto.screener.config import ScreenerConfig
    from ditto.screener.gate import BuildGate
    from ditto.screener.platform import PlatformClient

logger = logging.getLogger(__name__)


class ScreenerWorker:
    """Drains the screener queue, gating each agent and posting a verdict."""

    def __init__(
        self,
        *,
        config: ScreenerConfig,
        platform: PlatformClient,
        gate: BuildGate,
        keypair: Any,
    ) -> None:
        self._config = config
        self._platform = platform
        self._gate = gate
        self._keypair = keypair

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep until ``stop`` is set, sleeping when the queue is empty."""
        logger.info(
            "screener worker started hotkey=%s netuid=%d platform=%s",
            self._config.screener_hotkey,
            self._config.netuid,
            self._config.platform_api_url,
        )
        while not stop.is_set():
            try:
                processed = await self._sweep(stop)
            except PlatformError as e:
                logger.warning("sweep failed (retrying next cycle): %s", e)
                processed = 0
            if processed == 0 and not stop.is_set():
                await self._sleep_or_stop(stop, self._config.poll_seconds)
        logger.info("screener worker stopped")

    async def _sweep(self, stop: asyncio.Event) -> int:
        """Lease and screen the next eligible agent; return how many were done."""
        queue = await self._platform.claim_next()
        if queue.required_policy_version > SCREENING_POLICY_VERSION:
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

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early if ``stop`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

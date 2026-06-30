"""The validator epoch loop: queue -> score -> weights.

One sweep: pull agents in ``evaluating`` from the platform, score each through
dittobench-api (by presigned tarball URL), report the signed score back, then
set chain weights from the per-miner composites. Failures scoring one agent are
logged and skipped — one bad submission must not stall the sweep or block
weight-setting for everyone else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ditto.validator.errors import DittobenchError, PlatformError
from ditto.validator.signing import sign_score
from ditto.validator.weights import compute_weights

if TYPE_CHECKING:
    from ditto.api_models.validator import ScoreReport, ValidatorQueueItem
    from ditto.chain import ChainClient
    from ditto.validator.config import ValidatorConfig
    from ditto.validator.dittobench import DittobenchClient
    from ditto.validator.platform import PlatformClient

logger = logging.getLogger(__name__)


class ValidatorWorker:
    """Owns one scoring sweep and the long-lived loop around it."""

    def __init__(
        self,
        config: ValidatorConfig,
        platform: PlatformClient,
        dittobench: DittobenchClient,
        chain: ChainClient,
        keypair: Any,
    ) -> None:
        self._config = config
        self._platform = platform
        self._dittobench = dittobench
        self._chain = chain
        self._keypair = keypair

    async def run_once(self) -> int:
        """Run one full sweep. Returns the number of agents pulled from the queue."""
        queue = await self._platform.get_queue()
        if not queue.items:
            logger.info("queue empty; nothing to score this sweep")
            return 0

        scores: dict[str, float] = {}
        for item in queue.items:
            try:
                report = await self._score_agent(item)
            except (DittobenchError, PlatformError) as e:
                logger.warning("scoring agent %s failed: %s", item.agent_id, e)
                continue
            # Last writer wins if a miner somehow appears twice in one sweep.
            scores[item.miner_hotkey] = report.composite

        if scores:
            weights = compute_weights(scores)
            if weights:
                await self._chain.put_weights(weights)
                logger.info("submitted weights for %d miner(s)", len(weights))
            else:
                logger.info("no positive scores; skipping put_weights")
        return len(queue.items)

    async def _score_agent(self, item: ValidatorQueueItem) -> ScoreReport:
        artifact = await self._platform.get_artifact(item.agent_id)
        report = await self._dittobench.score_tarball(tarball_url=artifact.download_url)
        signature = sign_score(
            self._keypair, self._config.validator_hotkey, report.run_id
        )
        await self._platform.submit_score(
            item.agent_id, signature=signature, report=report
        )
        logger.info(
            "scored agent %s (miner=%s composite=%.3f)",
            item.agent_id,
            item.miner_hotkey,
            report.composite,
        )
        return report

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sweep, then sleep ~epoch, until ``stop`` is set (SIGTERM drain)."""
        while not stop.is_set():
            try:
                n = await self.run_once()
                logger.info("sweep complete: %d agent(s)", n)
            except Exception:  # noqa: BLE001 - a sweep must never kill the loop
                logger.exception("sweep failed; retrying next epoch")
            await self._sleep_or_stop(stop, self._config.epoch_seconds)

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
        """Sleep up to ``seconds``, returning early if ``stop`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

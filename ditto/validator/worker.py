"""The validator epoch loop: queue -> score -> weights.

One sweep: pull agents in ``evaluating`` from the platform, score each through
dittobench-api (by presigned tarball URL), and report the signed score back.
Weight-setting is **decoupled** from that sweep: weights are recomputed from the
platform's persistent best-score *ledger* (``/scoring/scores``) and set every
epoch — even when nothing new was scored — via the KOTH+ATH mechanism. This is
the fix for the one-epoch-weight bug: the old loop built weights only from the
current ``evaluating`` set, so a scored agent (which leaves that queue) was
zeroed the next epoch. Failures scoring one agent are logged and skipped — one
bad submission must not stall the sweep or block weight-setting for everyone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ditto.chain import ChainError
from ditto.validator.errors import (
    DittobenchError,
    PlatformError,
    WeightSubmissionError,
)
from ditto.validator.signing import sign_score
from ditto.validator.telemetry import (
    ScoredAgentStat,
    SweepStats,
    TelemetryConfig,
    ValidatorTelemetry,
    scored_agent_stat,
)
from ditto.validator.weights import compute_weights

if TYPE_CHECKING:
    from ditto.api_models.validator import ScoreReport, ValidatorQueueItem
    from ditto.chain import ChainClient
    from ditto.validator.config import ValidatorConfig
    from ditto.validator.dittobench import DittobenchClient
    from ditto.validator.platform import PlatformClient

logger = logging.getLogger(__name__)

# A transient chain/Pylon failure setting weights is retried a few times within
# the epoch; the ledger is durable so the next epoch recovers regardless.
_WEIGHT_SET_ATTEMPTS = 3
_WEIGHT_SET_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class _WeightOutcome:
    """What :meth:`ValidatorWorker._update_weights` produced, for telemetry."""

    leaderboard: list[tuple[str, float]] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    submitted: bool = False


class ValidatorWorker:
    """Owns one scoring sweep and the long-lived loop around it."""

    def __init__(
        self,
        config: ValidatorConfig,
        platform: PlatformClient,
        dittobench: DittobenchClient,
        chain: ChainClient | None,
        keypair: Any,
        weight_setter: Any | None = None,
        telemetry: ValidatorTelemetry | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._dittobench = dittobench
        self._chain = chain
        self._keypair = keypair
        # The weight sink: the Pylon-backed ChainClient by default, or an
        # injected setter (e.g. the bittensor-SDK fallback on the localnet).
        # Both expose ``async def put_weights(dict[str, float])``.
        self._weight_setter: Any = weight_setter if weight_setter is not None else chain
        # Public telemetry sink. A disabled instance is a cheap no-op, so the
        # sweep can call it unconditionally.
        self._telemetry: ValidatorTelemetry = telemetry or ValidatorTelemetry(
            TelemetryConfig(mode="disabled", project="", entity=None, run_name=None),
            validator_hotkey=config.validator_hotkey,
            netuid=config.netuid,
        )

    async def run_once(self) -> int:
        """Run one full sweep. Returns the number of agents pulled from the queue.

        Scoring persists each agent's composite to the platform; weight-setting
        then reads the durable ledger and runs every epoch (see
        :meth:`_update_weights`), so an empty queue no longer means "set no
        weights" — the reigning champion keeps its emission.
        """
        started = time.monotonic()
        queue = await self._platform.get_queue()
        scored: list[ScoredAgentStat] = []
        failed = 0
        for item in queue.items:
            try:
                report = await self._score_agent(item)
                scored.append(scored_agent_stat(item.miner_hotkey, report))
            except (DittobenchError, PlatformError) as e:
                logger.warning("scoring agent %s failed: %s", item.agent_id, e)
                failed += 1
                continue

        outcome = await self._update_weights()
        self._telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=time.monotonic() - started,
                queue_depth=len(queue.items),
                failed_count=failed,
                scored=scored,
                leaderboard=outcome.leaderboard,
                weights=outcome.weights,
                weights_submitted=outcome.submitted,
            )
        )
        return len(queue.items)

    async def _update_weights(self) -> _WeightOutcome:
        """Recompute weights from the durable ledger and submit them.

        Reads the platform's best-score-per-miner ledger and folds it into the
        KOTH+ATH weight vector. On a ledger-read failure it leaves the current
        on-chain weights untouched (rather than zeroing everyone) and lets the
        next epoch retry. Returns what happened (leaderboard + weights + whether
        submitted) for telemetry.
        """
        try:
            ledger = await self._platform.get_ledger()
        except PlatformError as e:
            logger.warning("ledger fetch failed; weights unchanged this epoch: %s", e)
            return _WeightOutcome()

        leaderboard = [(e.miner_hotkey, e.composite) for e in ledger.entries]
        weights = compute_weights(
            ledger.entries,
            margin=self._config.koth_margin,
            tail_size=self._config.koth_tail_size,
            champion_share=self._config.koth_champion_share,
        )
        if not weights:
            logger.info("ledger has no positive scores; skipping put_weights")
            return _WeightOutcome(leaderboard=leaderboard)
        submitted = await self._put_weights_with_retry(weights)
        return _WeightOutcome(
            leaderboard=leaderboard, weights=weights, submitted=submitted
        )

    async def _put_weights_with_retry(self, weights: dict[str, float]) -> bool:
        """Submit weights, retrying a transient chain failure a few times.

        The ledger is durable, so even if every attempt fails the next epoch
        recomputes and retries from the same persisted scores — a chain blip
        never permanently drops a miner (the failure mode of the old per-sweep
        composite dict).
        """
        for attempt in range(1, _WEIGHT_SET_ATTEMPTS + 1):
            try:
                await self._weight_setter.put_weights(weights)
                logger.info("submitted weights for %d miner(s)", len(weights))
                return True
            except (ChainError, WeightSubmissionError) as e:
                if attempt >= _WEIGHT_SET_ATTEMPTS:
                    logger.error(
                        "put_weights failed after %d attempt(s); next epoch "
                        "retries from the ledger: %s",
                        attempt,
                        e,
                    )
                    return False
                logger.warning(
                    "put_weights attempt %d/%d failed; retrying: %s",
                    attempt,
                    _WEIGHT_SET_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(_WEIGHT_SET_RETRY_SECONDS)
        return False

    async def _score_agent(self, item: ValidatorQueueItem) -> ScoreReport:
        artifact = await self._platform.get_artifact(item.agent_id)
        # The queue item and the artifact response both carry the registered
        # digest; a mismatch means the platform is inconsistent about which blob
        # this agent is, so refuse to score rather than sign a score for an
        # ambiguous artifact. (The scorer re-verifies the bytes too — this is the
        # cheap cross-check before we even hand off the URL.)
        if item.sha256.lower() != artifact.sha256.lower():
            raise PlatformError(
                f"sha256 mismatch for agent {item.agent_id}: "
                f"queue={item.sha256} artifact={artifact.sha256}"
            )
        report = await self._dittobench.score_tarball(
            tarball_url=artifact.download_url, tarball_sha256=artifact.sha256
        )
        signature = sign_score(
            self._keypair,
            validator_hotkey=self._config.validator_hotkey,
            agent_id=item.agent_id,
            run_id=report.run_id,
            composite=report.composite,
            seed=report.seed,
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

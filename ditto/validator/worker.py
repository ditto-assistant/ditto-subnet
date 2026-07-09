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
import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ditto.chain import ChainError
from ditto.validator.crn import crn_seed
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
from ditto.validator.weights import (
    DEFAULT_BENCH_VERSION,
    agents_needing_rescore,
    compute_weights,
)

if TYPE_CHECKING:
    from uuid import UUID

    from ditto.api_models.validator import (
        JobResponse,
        LedgerResponse,
        ScoreReport,
    )
    from ditto.chain import ChainClient
    from ditto.validator.config import ValidatorConfig
    from ditto.validator.dittobench import DittobenchClient
    from ditto.validator.platform import PlatformClient

logger = logging.getLogger(__name__)

# A transient chain/Pylon failure setting weights is retried a few times within
# the epoch; the ledger is durable so the next epoch recovers regardless.
# Retries back off exponentially (base * 2**(attempt-1)); a rate-limit
# rejection uses the longer block-time base since retrying inside the same
# block is a guaranteed second rejection.
_WEIGHT_SET_ATTEMPTS = 3
_WEIGHT_SET_RETRY_SECONDS = 2.0
_WEIGHT_SET_RATE_LIMIT_RETRY_SECONDS = 12.0

# Substrate block time; converts the chain's block-denominated
# ``weights_rate_limit`` into the loop's seconds-denominated cadence.
_BLOCK_SECONDS = 12.0

# Substrings that identify a chain rate-limit rejection across the surfaces we
# submit through (subtensor's ``SettingWeightsTooFast`` error, SDK / Pylon
# message variants).
_RATE_LIMIT_MARKERS = ("rate limit", "ratelimit", "too fast", "toofast")


def _is_rate_limit_error(error: Exception) -> bool:
    """Whether a weight-submission failure looks like a chain rate-limit."""
    message = str(error).lower()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


def _retry_delay_seconds(attempt: int, error: Exception) -> float:
    """Backoff before retry ``attempt + 1``: exponential over the error's base."""
    base = (
        _WEIGHT_SET_RATE_LIMIT_RETRY_SECONDS
        if _is_rate_limit_error(error)
        else _WEIGHT_SET_RETRY_SECONDS
    )
    return base * 2 ** (attempt - 1)


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
        # The newest bench_version this validator's scorer has produced (learned
        # from each scored run's details). Drives the §9 re-score sweep: ledger
        # entries scored below this are stale and re-evaluated before the fold.
        # Starts at the baseline so a just-booted validator that has not scored
        # anything yet never mistakes the whole ledger for stale.
        self._current_bench_version = DEFAULT_BENCH_VERSION

    async def run_once(self, *, set_weights: bool = True) -> int:
        """Run one full sweep. Returns the number of agents pulled from the queue.

        Which halves run is gated by this instance's role (``enable_scoring`` /
        ``enable_weights``):

        * Scoring (``enable_scoring``): pull the ``evaluating`` queue, score each
          agent through dittobench-api, persist the signed composite, and
          re-score stale champions. The central scorer runs only this.
        * Weights (``enable_weights`` and ``set_weights``): recompute weights from
          the durable ledger and submit them (see :meth:`_update_weights`), so an
          empty queue no longer means "set no weights" — the reigning champion
          keeps its emission. An independent (weights-only) validator runs only
          this, reading the ledger the central scorer maintains.

        ``run_forever`` scores every sweep but only sets weights when the epoch
        interval is due, so scoring latency isn't tied to the longer weight
        cadence.
        """
        started = time.monotonic()
        scored: list[ScoredAgentStat] = []
        failed = 0
        queue_depth = 0
        if self._config.enable_scoring:
            # k=3 pull: request tickets until the platform says 204 (no work for
            # us) or this sweep's cap is hit. Each ticket pins the dataset all
            # three validators score, so scores stay comparable for the median.
            while queue_depth < self._config.queue_limit:
                job = await self._platform.request_job()
                if job is None:
                    break  # 204: no ticket available
                queue_depth += 1
                if job.deadline <= datetime.now(UTC):
                    # Already lapsed (the platform will re-open it); don't waste a
                    # full run on a score that would be invalidated as late.
                    logger.warning(
                        "ticket for agent %s already past deadline %s; skipping",
                        job.agent_id,
                        job.deadline.isoformat(),
                    )
                    continue
                try:
                    report = await self._score_job(job)
                    details = getattr(self._dittobench, "last_details", None)
                    if not isinstance(details, dict):
                        details = {}
                    scored.append(scored_agent_stat(job.miner_hotkey, report, details))
                except (DittobenchError, PlatformError) as e:
                    logger.warning("scoring agent %s failed: %s", job.agent_id, e)
                    failed += 1
                    continue
            # Re-scoring stale champions is a scoring responsibility, done here
            # (not in the weight fold) so the split topology keeps re-scoring:
            # the weight-only validators just read the ledger the scorer refreshes.
            await self._rescore_stale_champions()

        outcome = _WeightOutcome()
        if set_weights and self._config.enable_weights:
            outcome = await self._update_weights()
        self._telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=time.monotonic() - started,
                queue_depth=queue_depth,
                failed_count=failed,
                scored=scored,
                leaderboard=outcome.leaderboard,
                weights=outcome.weights,
                weights_submitted=outcome.submitted,
            )
        )
        return queue_depth

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

        # Re-scoring stale champions (§9 version-bump) is the scorer's job now,
        # run in the scoring sweep (see _rescore_stale_champions); the fold reads
        # whatever the scorer has already persisted. compute_weights ignores
        # stale versions defensively regardless.
        leaderboard = [(e.miner_hotkey, e.composite) for e in ledger.entries]
        weights = compute_weights(
            ledger.entries,
            margin=self._config.koth_margin,
            tail_size=self._config.koth_tail_size,
            champion_share=self._config.koth_champion_share,
            dethrone_z=self._config.koth_dethrone_z,
        )
        if not weights:
            logger.info("ledger has no positive scores; skipping put_weights")
            return _WeightOutcome(leaderboard=leaderboard)
        if not await self._validator_permitted() or not await self._stake_sufficient():
            # No permit / demonstrably short stake → the chain would reject the
            # submission anyway; skip it (loudly) rather than burn an epoch on a
            # guaranteed rejection.
            return _WeightOutcome(leaderboard=leaderboard, weights=weights)
        await self._log_commit_reveal_mode()
        submitted = await self._put_weights_with_retry(weights)
        return _WeightOutcome(
            leaderboard=leaderboard, weights=weights, submitted=submitted
        )

    async def _rescore_stale_champions(self) -> None:
        """Read the ledger and re-score any champion/tail agents scored under an
        older bench_version than this scorer now produces (BENCHMARK-V2 §9).

        Run in the scoring sweep so the durable ledger the weight fold reads is
        already refreshed, which keeps re-scoring working once scoring and
        weight-setting live in separate processes. Inert until the platform
        surfaces per-entry versions; one agent failing to re-score is logged and
        skipped. A ledger-read failure is swallowed — the next sweep retries.
        """
        try:
            ledger = await self._platform.get_ledger()
        except PlatformError as e:
            logger.warning("ledger fetch for re-score failed; skipping: %s", e)
            return
        await self._rescore_stale_champion_and_tail(ledger)

    async def _rescore_stale_champion_and_tail(
        self, ledger: LedgerResponse
    ) -> LedgerResponse:
        """Re-evaluate the champion + participation-tail agents whose ledger
        bench_version is older than this validator's current scorer version
        (BENCHMARK-V2 §9 step 2), then re-fetch the ledger so the fold sees the
        refreshed scores. A no-op — with no re-fetch — when the ledger carries no
        per-entry version (the platform surfacing it is optional per §7) or when
        nothing is stale. One agent failing to re-score is logged and skipped; it
        must never stall weight-setting.
        """
        entries = ledger.entries
        # Only act once the ledger actually distinguishes versions; otherwise we
        # cannot tell stale from current and must not re-score on every epoch.
        if not any(getattr(e, "bench_version", None) is not None for e in entries):
            return ledger
        stale = agents_needing_rescore(
            entries,
            current_version=self._current_bench_version,
            margin=self._config.koth_margin,
            tail_size=self._config.koth_tail_size,
            dethrone_z=self._config.koth_dethrone_z,
        )
        if not stale:
            return ledger
        # v3 #1 (CRN): score the whole stale champion+tail set on ONE deterministic
        # common seed so their refreshed composites face the identical dataset and
        # become directly comparable. The seed is a pure hash of the compared
        # agent ids + version, so every validator derives the same one (consensus-
        # safe) — see ditto/validator/crn.py.
        sweep_seed = crn_seed(
            (str(e.agent_id) for e in stale), version=self._current_bench_version
        )
        logger.info(
            "bench_version %d re-score sweep: %d stale champion/tail agent(s) "
            "(CRN seed=%d)",
            self._current_bench_version,
            len(stale),
            sweep_seed,
        )
        rescored = 0
        for e in stale:
            try:
                await self._evaluate_and_submit(
                    e.agent_id, e.sha256, e.miner_hotkey, seed=sweep_seed
                )
                rescored += 1
            except (PlatformError, DittobenchError) as exc:
                logger.warning(
                    "re-score of stale agent %s failed; leaving its ledger score: %s",
                    e.agent_id,
                    exc,
                )
        if rescored == 0:
            return ledger
        try:
            return await self._platform.get_ledger()
        except PlatformError as exc:
            logger.warning(
                "ledger re-fetch after re-score failed; folding pre-re-score: %s",
                exc,
            )
            return ledger

    async def _validator_permitted(self) -> bool:
        """Best-effort self-check that our hotkey may set weights this epoch.

        Reads the metagraph through whichever weight sink is active (the Pylon
        ``ChainClient`` or the SDK setter — both expose ``has_validator_permit``)
        and skips submission when the validator hotkey demonstrably lacks a
        ``validator_permit``. **Fail-open:** if the check is unavailable or
        errors (undeterminable, transient chain read), proceed and let the chain
        enforce — the goal is a clear log line, not a second gate that can wedge
        weight-setting on a flaky read.
        """
        check = getattr(self._weight_setter, "has_validator_permit", None)
        if check is None:
            return True
        hotkey = self._config.validator_hotkey
        netuid = self._config.netuid
        try:
            result = check(hotkey, netuid)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # noqa: BLE001 - a flaky read must not wedge weights
            logger.warning("validator permit self-check errored (%s); proceeding", e)
            return True
        if result is False:
            logger.warning(
                "validator hotkey %s lacks a validator_permit on netuid %s; "
                "skipping weight submission (stake below the permit threshold?)",
                hotkey,
                netuid,
            )
            return False
        if result is None:
            logger.info(
                "validator hotkey %s not found on netuid %s metagraph; "
                "proceeding (chain enforces)",
                hotkey,
                netuid,
            )
        return True

    async def _stake_sufficient(self) -> bool:
        """Best-effort self-check that our hotkey clears the min-stake bar.

        The companion arm to :meth:`_validator_permitted`: when
        ``VALIDATOR_MIN_STAKE_TAO`` is set (> 0), read our own stake through the
        weight sink and skip submission when it is demonstrably below the
        threshold. Same **fail-open** posture as the permit check — an
        unavailable or failing read proceeds and lets the chain enforce.
        """
        min_stake = self._config.min_stake_tao
        if min_stake <= 0:
            return True
        read = getattr(self._weight_setter, "get_stake_tao", None)
        if read is None:
            return True
        hotkey = self._config.validator_hotkey
        netuid = self._config.netuid
        try:
            stake = read(hotkey, netuid)
            if inspect.isawaitable(stake):
                stake = await stake
        except Exception as e:  # noqa: BLE001 - a flaky read must not wedge weights
            logger.warning("stake self-check errored (%s); proceeding", e)
            return True
        if stake is None:
            logger.info(
                "validator hotkey %s not found on netuid %s metagraph; "
                "proceeding (chain enforces)",
                hotkey,
                netuid,
            )
            return True
        if stake < min_stake:
            logger.warning(
                "validator hotkey %s stake %.4f TAO is below the configured "
                "minimum %.4f TAO on netuid %s; skipping weight submission",
                hotkey,
                stake,
                min_stake,
                netuid,
            )
            return False
        return True

    async def _log_commit_reveal_mode(self) -> None:
        """Observe + log whether this network runs commit-reveal.

        Under commit-reveal v3 the active weight sink (``set_weights`` or Pylon)
        does the timelock commit itself and the chain auto-reveals after
        ``RevealPeriodEpochs`` — there is **no** separate reveal call for the
        worker to make. This method only *reports* the mode so a cutover can
        confirm commit-reveal is actually on; without it weights are copy-able
        (front-runnable). When ``VALIDATOR_REQUIRE_COMMIT_REVEAL`` is set and the
        chain reports commit-reveal off, it logs an error but still submits —
        refusing would zero the chain, a worse failure. **Fail-open:** any read
        error or a sink without the reader is a silent no-op.
        """
        read_enabled = getattr(self._weight_setter, "get_commit_reveal_enabled", None)
        if read_enabled is None:
            return
        netuid = self._config.netuid
        try:
            enabled = read_enabled(netuid)
            if inspect.isawaitable(enabled):
                enabled = await enabled
        except Exception as e:  # noqa: BLE001 - observability must not wedge weights
            logger.warning("commit-reveal self-check errored (%s); proceeding", e)
            return
        # Real sinks return bool | None; be defensive about anything else.
        if enabled is not None and not isinstance(enabled, bool):
            enabled = None
        if enabled is None:
            log = logger.warning if self._config.require_commit_reveal else logger.info
            log("commit-reveal state undeterminable on netuid %s; proceeding", netuid)
            return
        if enabled:
            period = await self._read_reveal_period(netuid)
            logger.info(
                "commit-reveal ON (netuid %s, reveal period %s epochs): weights are "
                "committed now and revealed on-chain after the reveal window",
                netuid,
                period if period is not None else "?",
            )
        elif self._config.require_commit_reveal:
            logger.error(
                "commit-reveal is OFF on netuid %s but VALIDATOR_REQUIRE_COMMIT_REVEAL "
                "is set — weights are front-runnable; enable the "
                "CommitRevealWeightsEnabled hyperparameter. Submitting anyway "
                "(refusing would zero the chain)",
                netuid,
            )
        else:
            logger.info(
                "commit-reveal OFF on netuid %s: weights applied directly", netuid
            )

    async def _read_reveal_period(self, netuid: int) -> int | None:
        """Best-effort read of ``RevealPeriodEpochs`` for the mode log (advisory)."""
        read = getattr(self._weight_setter, "get_reveal_period_epochs", None)
        if read is None:
            return None
        try:
            period = read(netuid)
            if inspect.isawaitable(period):
                period = await period
        except Exception:  # noqa: BLE001 - advisory only
            return None
        return period if isinstance(period, int) else None

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
                delay = _retry_delay_seconds(attempt, e)
                logger.warning(
                    "put_weights attempt %d/%d failed%s; retrying in %.1fs: %s",
                    attempt,
                    _WEIGHT_SET_ATTEMPTS,
                    " (rate-limited)" if _is_rate_limit_error(e) else "",
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
        return False

    async def _score_job(self, job: JobResponse) -> ScoreReport:
        """Score one issued ticket against its platform-pinned dataset."""
        return await self._evaluate_and_submit(
            job.agent_id,
            job.sha256,
            job.miner_hotkey,
            seed=job.seed,
            dataset_sha256=job.dataset_sha256,
            run_size=job.run_size,
        )

    async def _evaluate_and_submit(
        self,
        agent_id: UUID,
        expected_sha256: str,
        miner_hotkey: str,
        *,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
    ) -> ScoreReport:
        """Fetch an agent's artifact, score it, sign, and submit. Shared by the
        ticket sweep (:meth:`_score_job`) and the §9 version-bump re-score.

        ``seed`` pins the dataset seed. ``dataset_sha256`` (from the ticket)
        selects the canonical /v1/score path, where the engine regenerates that
        exact dataset and fails on a hash mismatch (tamper-evidence). The re-score
        sweep passes a common ``seed`` (CRN) but no ``dataset_sha256`` (its
        comparison is across a fresh common dataset, not a platform-pinned one)."""
        artifact = await self._platform.get_artifact(agent_id)
        # The caller and the artifact response both carry the registered digest; a
        # mismatch means the platform is inconsistent about which blob this agent
        # is, so refuse to score rather than sign a score for an ambiguous
        # artifact. (The scorer re-verifies the bytes too — this is the cheap
        # cross-check before we even hand off the URL.)
        if expected_sha256.lower() != artifact.sha256.lower():
            raise PlatformError(
                f"sha256 mismatch for agent {agent_id}: "
                f"expected={expected_sha256} artifact={artifact.sha256}"
            )
        report = await self._dittobench.score_tarball(
            tarball_url=artifact.download_url,
            tarball_sha256=artifact.sha256,
            seed=seed,
            dataset_sha256=dataset_sha256,
            run_size=run_size,
        )
        signature = sign_score(
            self._keypair,
            validator_hotkey=self._config.validator_hotkey,
            agent_id=agent_id,
            run_id=report.run_id,
            composite=report.composite,
            seed=report.seed,
        )
        await self._platform.submit_score(agent_id, signature=signature, report=report)
        details = getattr(self._dittobench, "last_details", None)
        bench_version = (
            details.get("bench_version") if isinstance(details, dict) else None
        )
        # Learn the scorer's current bench_version so the re-score sweep knows
        # which ledger entries are stale.
        if (
            isinstance(bench_version, int)
            and bench_version > self._current_bench_version
        ):
            self._current_bench_version = bench_version
        logger.info(
            "scored agent %s (miner=%s composite=%.3f bench_version=%s)",
            agent_id,
            miner_hotkey,
            report.composite,
            bench_version,
        )
        return report

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Score every ``sweep_seconds``; set weights every effective epoch.

        Scoring cadence is decoupled from the weight-set cadence: the queue is
        drained promptly (a submission is scored within ~one sweep) while
        weights are pushed no more often than the effective epoch interval —
        the configured ``epoch_seconds`` stretched to the subnet's on-chain
        ``weights_rate_limit`` window (re-read once per epoch) — so the loop
        doesn't fight the chain's rate limiter. The first sweep sets weights so
        a fresh start doesn't wait a full epoch. Runs until ``stop`` is set
        (SIGTERM drain).
        """
        last_weight_set: float | None = None
        chain_floor = await self._chain_min_epoch_seconds()
        while not stop.is_set():
            epoch_seconds = max(float(self._config.epoch_seconds), chain_floor)
            due = (
                last_weight_set is None
                or time.monotonic() - last_weight_set >= epoch_seconds
            )
            try:
                n = await self.run_once(set_weights=due)
                if due:
                    last_weight_set = time.monotonic()
                    # Once per epoch is a cheap read and tracks a live
                    # hyperparameter change within one weight-set window.
                    chain_floor = await self._chain_min_epoch_seconds()
                logger.info(
                    "sweep complete: %d agent(s)%s",
                    n,
                    " (weights set)" if due else "",
                )
            except Exception:  # noqa: BLE001 - a sweep must never kill the loop
                logger.exception("sweep failed; retrying next sweep")
            await self._sleep_or_stop(stop, self._config.sweep_seconds)

    async def _chain_min_epoch_seconds(self) -> float:
        """The chain-enforced floor (seconds) on the weight-set cadence.

        Reads the subnet's ``weights_rate_limit`` (and ``tempo``, for the log
        line) through the active weight sink and converts blocks to seconds.
        Replaces the hand-set ``VALIDATOR_EPOCH_SECONDS``-only proxy: the loop
        uses ``max(epoch_seconds, this floor)``. **Fail-open:** an unavailable
        or failing read returns ``0.0`` so the configured cadence still drives
        the loop.
        """
        rate_limit = await self._read_chain_blocks("get_weights_rate_limit")
        if rate_limit is None:
            return 0.0
        tempo = await self._read_chain_blocks("get_tempo")
        floor = float(rate_limit) * _BLOCK_SECONDS
        log = logger.warning if floor > self._config.epoch_seconds else logger.info
        log(
            "chain cadence for netuid %s: weights_rate_limit=%d block(s) "
            "(~%.0fs) tempo=%s block(s); configured epoch_seconds=%d -> "
            "effective %.0fs",
            self._config.netuid,
            rate_limit,
            floor,
            tempo if tempo is not None else "?",
            self._config.epoch_seconds,
            max(float(self._config.epoch_seconds), floor),
        )
        return floor

    async def _read_chain_blocks(self, method_name: str) -> int | None:
        """Call an optional block-count read on the weight sink, fail-open."""
        read = getattr(self._weight_setter, method_name, None)
        if read is None:
            return None
        try:
            result = read(self._config.netuid)
            if inspect.isawaitable(result):
                result = await result
            return None if result is None else int(result)
        except Exception as e:  # noqa: BLE001 - a flaky read must not wedge the loop
            logger.warning("%s errored (%s); using configured cadence", method_name, e)
            return None

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
        """Sleep up to ``seconds``, returning early if ``stop`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

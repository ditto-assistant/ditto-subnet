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
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    BenchmarkProgressStage,
)
from ditto.api_models.validator import ValidatorHeartbeatRequest, ValidatorRuntimeState
from ditto.chain import ChainError
from ditto.validator.build_info import validator_build_info
from ditto.validator.crn import confirmation_seeds
from ditto.validator.errors import (
    DittobenchError,
    PlatformError,
    ValidatorInfrastructureError,
    WeightSubmissionError,
)
from ditto.validator.onchain_seed import seed_matches
from ditto.validator.signing import sign_heartbeat, sign_score
from ditto.validator.telemetry import (
    ScoredAgentStat,
    SweepStats,
    TelemetryConfig,
    ValidatorTelemetry,
    scored_agent_stat,
)
from ditto.validator.update_control import write_update_state
from ditto.validator.weights import (
    DEFAULT_BENCH_VERSION,
    agents_needing_rescore,
    apply_miner_emission_cap,
    compute_weights,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.validator import (
        JobResponse,
        LedgerResponse,
        ScoreReport,
    )
    from ditto.chain import ChainClient
    from ditto.system_health import SystemMetricsCollector
    from ditto.validator.config import ValidatorConfig
    from ditto.validator.dittobench import (
        DittobenchClient,
        DittobenchProgressSnapshot,
        ProgressCallback,
    )
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

# Keep a validator visibly online throughout a long full benchmark. This is a
# protocol cadence, not an operator tuning knob.
_ACTIVE_HEARTBEAT_SECONDS = 120.0
# Count-only updates are intentionally slower than the scorer's ten-second poll.
# Stage transitions still publish immediately.
_PROGRESS_UPDATE_SECONDS = 60.0
# Active ticket work must never wait on the platform client's normal HTTP timeout.
_ACTIVE_TELEMETRY_TIMEOUT_SECONDS = 2.0
# Keep a successfully reported generic failure visible through at least one
# progress reporting interval. A new ticket supersedes it immediately.
_FAILED_PROGRESS_MIN_VISIBLE_SECONDS = 60.0

_PROGRESS_STAGE_ORDER: dict[BenchmarkProgressStage, int] = {
    "preparing": 0,
    "building_harness": 1,
    "starting_harness": 2,
    "running_benchmark": 3,
    "finalizing": 4,
    "submitting_result": 5,
    "failed_retrying": 6,
}


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


def _pooled_confirmation_stderr(
    composites: Sequence[float], single_run_stderr: float | None
) -> float | None:
    """Standard error of a K-seed confirmation composite, pooling the seeds the
    re-score already runs.

    The KOTH z-band (:func:`ditto.validator.weights._beats`) gates a dethrone on
    ``composite_stderr``. A single run reports only its within-dataset sampling
    error and discards the between-seed spread the K confirmation seeds actually
    measure, so a re-score that runs K seeds still hands the fold a one-run band.
    This returns the LARGER of

      * the between-seed SEM ``stdev(composites) / sqrt(K)`` — the empirical
        reproducibility of the composite across the K common CRN seeds, and
      * a sampling floor ``single_run_stderr / sqrt(K)`` — the precision K pooled
        n-case runs give even when the seeds happen to agree,

    so the band tightens by ~``sqrt(K)`` in the good case but never collapses when
    a small K draws lucky-agreeing composites (which would let a verbatim copy
    dethrone on measurement noise). ``None`` for K < 2 (no between-seed estimate;
    the caller keeps the single run's stderr). Pure and deterministic."""
    k = len(composites)
    if k < 2:
        return None
    mean = sum(composites) / k
    var = sum((c - mean) ** 2 for c in composites) / (k - 1)
    between = math.sqrt(var / k)
    floor = single_run_stderr / math.sqrt(k) if single_run_stderr else 0.0
    return max(between, floor)


@dataclass(frozen=True)
class _WeightOutcome:
    """What :meth:`ValidatorWorker._update_weights` produced, for telemetry."""

    leaderboard: list[tuple[str, float]] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    submitted: bool = False


@dataclass(frozen=True)
class _SweepOutcome:
    """Queue depth and whether this sweep completed its requested weight path."""

    queue_depth: int
    weights_ran: bool


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
        system_metrics: SystemMetricsCollector | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._dittobench = dittobench
        self._chain = chain
        self._keypair = keypair
        # The weight sink: the Pylon-backed ChainClient by default, or an
        # injected setter (used in tests to substitute a fake).
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
        self._last_heartbeat_timestamp = 0
        self._platform_accepted = False
        # Cooperative updater drains are acknowledged only after both the
        # independent scoring and weight loops have finished their current
        # unit of work. These flags are mutated without an intervening await,
        # so their check/set transitions are atomic within this event loop.
        self._scoring_active = False
        self._weights_active = False
        self._active_agent_id: UUID | None = None
        self._active_ticket_deadline: datetime | None = None
        self._benchmark_progress: BenchmarkProgress | None = None
        self._last_progress_heartbeat_monotonic: float | None = None
        self._last_progress_bucket: int | None = None
        self._active_heartbeat_lock = asyncio.Lock()
        self._retain_failed_progress_until = 0.0
        self._system_metrics = system_metrics

    async def run_once(
        self,
        *,
        set_weights: bool = True,
        stop_requested: asyncio.Event | None = None,
        drain_requested: asyncio.Event | None = None,
    ) -> _SweepOutcome:
        """Run one sweep and report queue depth plus weight-path completion.

        Every validator does both halves:

        * Scoring: pull the ``evaluating`` queue, score each agent through
          dittobench-api, persist the signed composite, and re-score stale
          champions.
        * Weights (when ``set_weights``): recompute weights from the durable
          ledger and submit them (see :meth:`_update_weights`), so an empty queue
          no longer means "set no weights": the reigning champion keeps its
          emission.

        ``run_forever`` scores every sweep but only sets weights when the epoch
        interval is due, so scoring latency isn't tied to the longer weight
        cadence.
        """
        started = time.monotonic()
        await self._report_heartbeat("polling")
        write_update_state("working", platform_accepted=self._platform_accepted)
        scored: list[ScoredAgentStat] = []
        failed = 0
        queue_depth = 0
        scoring_available = await self._scoring_preflight()
        if not scoring_available:
            failed = 1
        # k=3 pull: request tickets until the platform says 204 (no work for us)
        # or this sweep's cap is hit. Each ticket pins the dataset all three
        # validators score, so scores stay comparable for the median.
        while scoring_available and queue_depth < self._config.queue_limit:
            if self._new_work_blocked(stop_requested, drain_requested):
                break
            try:
                job = await self._platform.request_job()
            except PlatformError as e:
                # Scoring-plane availability must not gate the independent
                # weight path.  In particular, a platform validation/config
                # error can otherwise abort every due sweep before the durable
                # ledger (or its safe empty-ledger burn vector) reaches Pylon.
                logger.warning(
                    "job request failed; ending scoring sweep so weights can "
                    "proceed: %s",
                    e,
                )
                failed += 1
                break
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
            except ValidatorInfrastructureError as e:
                # The ticket remains leased until its existing deadline, then
                # the platform reopens the miner submission. Stop this sweep so
                # we neither blame the artifact nor immediately claim more work
                # against the same broken dependency.
                logger.warning(
                    "validator scoring infrastructure failed for agent %s; "
                    "leaving ticket to expire and ending scoring sweep: %s",
                    job.agent_id,
                    e,
                )
                failed += 1
                scoring_available = False
                break
            except (DittobenchError, PlatformError) as e:
                logger.warning("scoring agent %s failed: %s", job.agent_id, e)
                failed += 1
                continue
        if scoring_available and not self._new_work_blocked(
            stop_requested, drain_requested
        ):
            await self._rescore_stale_champions(
                stop_requested=stop_requested,
                drain_requested=drain_requested,
            )

        outcome = _WeightOutcome()
        weights_ran = False
        onchain_last_update_block: int | None = None
        onchain_observed_block: int | None = None
        if set_weights and not self._new_work_blocked(stop_requested, drain_requested):
            await self._report_heartbeat("updating_weights")
            outcome = await self._update_weights()
            (
                onchain_last_update_block,
                onchain_observed_block,
            ) = await self._observe_onchain_weight_state()
            weights_ran = True
        self._telemetry.record_sweep(
            SweepStats(
                sweep_duration_s=time.monotonic() - started,
                queue_depth=queue_depth,
                failed_count=failed,
                scored=scored,
                leaderboard=outcome.leaderboard,
                weights=outcome.weights,
                weights_submitted=outcome.submitted,
                weights_due=set_weights,
                burn_hotkey=self._config.burn_hotkey,
                onchain_last_update_block=onchain_last_update_block,
                onchain_observed_block=onchain_observed_block,
            )
        )
        await self._report_heartbeat("idle")
        return _SweepOutcome(queue_depth=queue_depth, weights_ran=weights_ran)

    async def _scoring_preflight(self) -> bool:
        """Functionally probe scorer dependencies before requesting a lease."""
        preflight = getattr(self._dittobench, "preflight", None)
        if preflight is None:
            return True
        try:
            result = preflight()
            if inspect.isawaitable(result):
                await result
            return True
        except ValidatorInfrastructureError as e:
            logger.warning(
                "validator scoring preflight failed; no ticket will be claimed "
                "this sweep: %s",
                e,
            )
            return False

    async def _report_heartbeat(
        self,
        state: ValidatorRuntimeState,
        *,
        active_snapshot: tuple[UUID | None, BenchmarkProgress | None] | None = None,
    ) -> bool:
        """Best-effort signed build + runtime report; never gate validator work."""
        if active_snapshot is None:
            active_agent_id = self._active_agent_id
            benchmark_progress = self._benchmark_progress
        else:
            active_agent_id, benchmark_progress = active_snapshot
        if (
            active_agent_id is None
            and time.monotonic() < self._retain_failed_progress_until
        ):
            return True
        try:
            build = validator_build_info()
            timestamp = max(int(time.time()), self._last_heartbeat_timestamp + 1)
            self._last_heartbeat_timestamp = timestamp
            system_metrics = (
                self._system_metrics.collect()
                if self._system_metrics is not None
                else None
            )
            signature = sign_heartbeat(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                software_version=build.software_version,
                protocol_version=build.protocol_version,
                code_digest=build.code_digest,
                state=state,
                active_agent_id=active_agent_id,
                system_metrics=system_metrics,
                benchmark_progress=benchmark_progress,
                timestamp=timestamp,
            )
            request = ValidatorHeartbeatRequest(
                validator_hotkey=self._config.validator_hotkey,
                software_version=build.software_version,
                protocol_version=build.protocol_version,
                code_digest=build.code_digest,
                state=state,
                active_agent_id=active_agent_id,
                system_metrics=system_metrics,
                benchmark_progress=benchmark_progress,
                timestamp=timestamp,
                signature=signature,
            )
            response = await self._platform.submit_heartbeat(request)
            # Update safety requires fresh platform acceptance. A later
            # rejection must revoke an earlier success instead of leaving the
            # updater-visible state permanently sticky.
            self._platform_accepted = response.accepted
            return response.accepted
        except Exception as e:  # noqa: BLE001 - observability must never gate work
            self._platform_accepted = False
            logger.warning("validator heartbeat failed (scoring continues): %s", e)
            return False

    async def _heartbeat_while_active(self, stop: asyncio.Event) -> None:
        """Refresh ``running_benchmark`` until the current scorer call ends."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_ACTIVE_HEARTBEAT_SECONDS)
            except TimeoutError:
                await self._emit_active_heartbeat()

    @staticmethod
    def _progress_bucket(progress: BenchmarkProgress) -> int | None:
        """Return the platform-facing five-percent bucket for throttling only."""
        if progress.completed is None or progress.total is None:
            return None
        percent = progress.completed * 100 // progress.total
        return min(100, percent // 5 * 5)

    async def _emit_active_heartbeat(self) -> bool:
        """Attempt one active heartbeat and remember its aggregate progress."""
        async with self._active_heartbeat_lock:
            active_snapshot = (self._active_agent_id, self._benchmark_progress)
            sent_progress = active_snapshot[1]
            delivered = await self._report_heartbeat_bounded(
                "running_benchmark", active_snapshot=active_snapshot
            )
            if delivered and sent_progress is not None:
                self._last_progress_heartbeat_monotonic = time.monotonic()
                self._last_progress_bucket = self._progress_bucket(sent_progress)
            return delivered

    async def _report_heartbeat_bounded(
        self,
        state: ValidatorRuntimeState,
        *,
        active_snapshot: tuple[UUID | None, BenchmarkProgress | None] | None = None,
    ) -> bool:
        """Bound telemetry I/O while a ticket is on the submission path."""
        try:
            return await asyncio.wait_for(
                self._report_heartbeat(state, active_snapshot=active_snapshot),
                timeout=_ACTIVE_TELEMETRY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("validator progress heartbeat timed out; scoring continues")
            return False

    async def _publish_benchmark_progress(
        self,
        stage: BenchmarkProgressStage,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> bool:
        """Cache safe progress and publish stage/count changes at bounded cadence."""
        if self._active_ticket_deadline is None or self._active_agent_id is None:
            return False
        try:
            previous = self._benchmark_progress
            if previous is not None:
                # DittoBench can briefly move from ``running`` back through its
                # internal ``seeding``/``generating`` phases. Public progress is
                # one monotonic lifecycle, so never regress a signed stage.
                if _PROGRESS_STAGE_ORDER[stage] < _PROGRESS_STAGE_ORDER[previous.stage]:
                    return False
                # An unstable/malformed same-stage poll must not erase a count
                # already accepted by the platform and later look like a
                # regression. Preserve the last safe aggregate instead.
                if (
                    stage == previous.stage
                    and completed is None
                    and total is None
                    and previous.completed is not None
                ):
                    completed = previous.completed
                    total = previous.total
            progress = BenchmarkProgress(
                stage=stage,
                completed=completed,
                total=total,
                ticket_deadline=self._active_ticket_deadline,
            )
            self._benchmark_progress = progress
            bucket = self._progress_bucket(progress)
            stage_changed = previous is None or previous.stage != progress.stage
            count_update_due = (
                not stage_changed
                and bucket is not None
                and bucket != self._last_progress_bucket
                and (
                    self._last_progress_heartbeat_monotonic is None
                    or time.monotonic() - self._last_progress_heartbeat_monotonic
                    >= _PROGRESS_UPDATE_SECONDS
                )
            )
            # The scorer's terminal failed poll is followed immediately by its
            # exception. Retry that one generic heartbeat so the exception path
            # knows whether a failure state was actually accepted before it
            # suppresses the clearing heartbeat for the visibility window.
            if stage_changed or count_update_due or stage == "failed_retrying":
                return await self._emit_active_heartbeat()
            return False
        except Exception:  # noqa: BLE001 - telemetry validation is fail-open
            logger.warning("benchmark progress update dropped; scoring continues")
            return False

    async def _on_dittobench_progress(
        self, snapshot: DittobenchProgressSnapshot
    ) -> None:
        """Map an already-sanitized scorer snapshot onto the signed heartbeat."""
        completed = snapshot.completed
        total = snapshot.total
        if snapshot.stage == "finalizing" and (
            completed is None or total is None or completed != total
        ):
            previous = self._benchmark_progress
            if (
                previous is None
                or previous.completed is None
                or previous.completed != previous.total
            ):
                return
            completed = previous.completed
            total = previous.total
        await self._publish_benchmark_progress(
            snapshot.stage, completed=completed, total=total
        )

    async def _begin_active_ticket(
        self, agent_id: UUID, ticket_deadline: datetime
    ) -> None:
        """Reset progress throttling and publish artifact preparation promptly."""
        self._retain_failed_progress_until = 0.0
        self._active_agent_id = agent_id
        self._active_ticket_deadline = ticket_deadline
        self._benchmark_progress = None
        self._last_progress_heartbeat_monotonic = None
        self._last_progress_bucket = None
        await self._publish_benchmark_progress("preparing")

    def _clear_active_ticket(self) -> None:
        self._active_agent_id = None
        self._active_ticket_deadline = None
        self._benchmark_progress = None
        self._last_progress_heartbeat_monotonic = None
        self._last_progress_bucket = None

    async def _update_weights(self) -> _WeightOutcome:
        """Recompute weights from the durable ledger and submit them.

        Reads the platform's best-score-per-miner ledger and folds it into the
        KOTH+ATH weight vector. Only miners present in the current metagraph enter
        that fold: Pylon also drops missing hotkeys during UID translation, but
        filtering first prevents a deregistered miner from retaining the crown or
        a tail slot and distorting the vector that remains. Platform submissions
        and scores stay durable, so the same hotkey becomes eligible again after
        re-registration. On a ledger or metagraph read failure it leaves the
        current on-chain weights untouched and lets the next epoch retry. Returns
        what happened (leaderboard + weights + whether submitted) for telemetry.
        """
        try:
            ledger = await self._platform.get_ledger()
        except PlatformError as e:
            logger.warning("ledger fetch failed; weights unchanged this epoch: %s", e)
            return _WeightOutcome()

        # The platform serves a last-known-good ledger (flagged stale) when its own
        # DB read fails; folding it is safe (the pool is durable + slow-moving) but
        # worth a loud line so an operator sees the platform is degraded.
        if getattr(ledger, "stale", False):
            logger.warning(
                "scoring ledger is STALE (platform served a %ss-old snapshot); "
                "folding it but the platform DB read is failing",
                getattr(ledger, "age_seconds", "?"),
            )

        eligible_entries = await self._registered_ledger_entries(ledger.entries)
        if eligible_entries is None:
            return _WeightOutcome()

        # Re-scoring stale champions (§9 version-bump) is the scorer's job now,
        # run in the scoring sweep (see _rescore_stale_champions); the fold reads
        # whatever the scorer has already persisted. compute_weights ignores
        # stale versions defensively regardless.
        leaderboard = [(e.miner_hotkey, e.composite) for e in ledger.entries]
        miner_weights = compute_weights(
            eligible_entries,
            margin=self._config.koth_margin,
            tail_size=self._config.koth_tail_size,
            champion_share=self._config.koth_champion_share,
            dethrone_z=self._config.koth_dethrone_z,
        )
        weights = apply_miner_emission_cap(
            miner_weights,
            miner_share=self._config.miner_emission_share,
            burn_hotkey=self._config.burn_hotkey,
        )
        if not miner_weights:
            logger.info(
                "ledger has no positive scores; routing 100% of miner emission to burn"
            )
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

    async def _registered_ledger_entries(
        self, entries: Sequence[Any]
    ) -> list[Any] | None:
        """Return ledger entries whose miner hotkeys have a current subnet UID.

        Production workers always have a :class:`ChainClient`; the non-awaitable
        fallback keeps lightweight injected test setters compatible. A real chain
        read failure is fail-closed for this epoch because folding the unfiltered
        ledger can let an absent hotkey hold the KOTH crown before Pylon silently
        drops it during hotkey-to-UID translation.
        """
        if self._chain is None:
            return list(entries)
        read = getattr(self._chain, "get_recent_neurons", None)
        if not callable(read):
            return list(entries)
        result = read(self._config.netuid)
        if not inspect.isawaitable(result):
            return list(entries)
        try:
            neurons = await result
        except ChainError as e:
            logger.warning(
                "metagraph fetch failed; weights unchanged this epoch: %s", e
            )
            return None

        registered = {neuron.hotkey for neuron in neurons}
        filtered = [e for e in entries if e.miner_hotkey in registered]
        missing = sorted({e.miner_hotkey for e in entries} - registered)
        if missing:
            logger.info(
                "excluding %d deregistered miner hotkey(s) from weight fold: %s",
                len(missing),
                ", ".join(missing),
            )
        return filtered

    async def _rescore_stale_champions(
        self,
        *,
        stop_requested: asyncio.Event | None = None,
        drain_requested: asyncio.Event | None = None,
    ) -> None:
        """Read the ledger and re-score any champion/tail agents scored under an
        older bench_version than this scorer now produces.

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
        await self._rescore_stale_champion_and_tail(
            ledger,
            stop_requested=stop_requested,
            drain_requested=drain_requested,
        )

    async def _rescore_stale_champion_and_tail(
        self,
        ledger: LedgerResponse,
        *,
        stop_requested: asyncio.Event | None = None,
        drain_requested: asyncio.Event | None = None,
    ) -> LedgerResponse:
        """Re-evaluate the champion + participation-tail agents whose ledger
        bench_version is older than this validator's current scorer version,
        then re-fetch the ledger so the fold sees the
        refreshed scores. A no-op — with no re-fetch — when the ledger carries no
        per-entry version (the platform surfacing it is optional) or when
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
        # CRN + P4: score the whole stale champion+tail set on K
        # deterministic COMMON seeds so their refreshed composites face identical
        # datasets and become directly comparable. Each seed is a pure hash of the
        # compared agent ids + version (+ replicate index), so every validator
        # derives the same set (consensus-safe) — see ditto/validator/crn.py. With
        # K >= 2 each agent is submitted once as the median over its seeds, so a
        # dethrone must replicate across seeds, not ride one lucky draw.
        sweep_seeds = confirmation_seeds(
            (str(e.agent_id) for e in stale),
            version=self._current_bench_version,
            count=self._config.koth_confirmation_seeds,
        )
        logger.info(
            "bench_version %d re-score sweep: %d stale champion/tail agent(s) "
            "(CRN seeds=%s)",
            self._current_bench_version,
            len(stale),
            sweep_seeds,
        )
        rescored = 0
        for e in stale:
            if self._new_work_blocked(stop_requested, drain_requested):
                break
            submitted = await self._confirm_and_submit(
                e.agent_id, e.sha256, e.miner_hotkey, seeds=sweep_seeds
            )
            if submitted is not None:
                rescored += 1
            else:
                logger.warning(
                    "re-score of stale agent %s produced no score; "
                    "leaving its ledger score",
                    e.agent_id,
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
        worker to make. Commit-reveal is not required: it is off by default, and
        this method only *reports* the mode (both states are logged at info) so a
        cutover can confirm what the network is running. **Fail-open:** any read
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
            logger.warning(
                "commit-reveal state undeterminable on netuid %s; proceeding", netuid
            )
            return
        if enabled:
            period = await self._read_reveal_period(netuid)
            logger.info(
                "commit-reveal ON (netuid %s, reveal period %s epochs): weights are "
                "committed now and revealed on-chain after the reveal window",
                netuid,
                period if period is not None else "?",
            )
        else:
            logger.info(
                "commit-reveal is OFF on netuid %s (not required); submitting "
                "weights directly",
                netuid,
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

    async def _observe_onchain_weight_state(self) -> tuple[int | None, int | None]:
        """Best-effort evidence for the latest weight update visible on-chain.

        Pylon's ``put_weights`` endpoint acknowledges a durable asynchronous
        request. Under commit-reveal that acknowledgement can precede the
        on-chain update by a full reveal window, so W&B must report both facts
        independently. A failed evidence read never blocks the weight loop.
        """
        read_update = getattr(self._weight_setter, "get_last_update_block", None)
        read_head = getattr(self._weight_setter, "get_latest_block", None)
        if read_update is None or read_head is None:
            return None, None
        try:
            last_update = read_update(
                self._config.validator_hotkey,
                self._config.netuid,
            )
            if inspect.isawaitable(last_update):
                last_update = await last_update
            head = read_head()
            if inspect.isawaitable(head):
                head = await head
            observed_block = getattr(head, "number", None)
            return (
                int(last_update) if last_update is not None else None,
                int(observed_block) if observed_block is not None else None,
            )
        except Exception as e:  # noqa: BLE001 - evidence must not wedge weights
            logger.warning("on-chain weight evidence read failed: %s", e)
            return None, None

    async def _score_job(self, job: JobResponse) -> ScoreReport:
        """Score one issued ticket against its platform-pinned dataset.

        When the ticket pins the seed's on-chain block hash, the seed is
        re-derived locally first (prod hardening P2): a mismatch means the
        platform issued a seed it could have chosen — refuse to score rather
        than lend the ticket a signature. Tickets without a block hash
        (pre-derivation agents) proceed as before.
        """
        if (
            job.seed is not None
            and job.dataset_seed_block_hash
            and not seed_matches(job.dataset_seed_block_hash, job.agent_id, job.seed)
        ):
            raise PlatformError(
                f"ticket seed {job.seed} for agent {job.agent_id} does not "
                f"re-derive from pinned block hash "
                f"{job.dataset_seed_block_hash!r}; refusing to score"
            )
        return await self._evaluate_and_submit(
            job.agent_id,
            job.sha256,
            job.miner_hotkey,
            seed=job.seed,
            dataset_sha256=job.dataset_sha256,
            run_size=job.run_size,
            ticket_deadline=job.deadline,
        )

    async def _evaluate(
        self,
        agent_id: UUID,
        expected_sha256: str,
        *,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
    ) -> ScoreReport:
        """Run an unticketed re-score with generic, agent-free heartbeats.

        ``seed`` pins the dataset seed. ``dataset_sha256`` (from the ticket)
        selects the canonical /v1/score path, where the engine regenerates that
        exact dataset and fails on a hash mismatch (tamper-evidence). The re-score
        sweep passes a common ``seed`` (CRN) but no ``dataset_sha256`` (its
        comparison is across a fresh common dataset, not a platform-pinned one)."""
        await self._report_heartbeat("running_benchmark")
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_while_active(heartbeat_stop)
        )
        try:
            return await self._evaluate_artifact(
                agent_id,
                expected_sha256,
                seed=seed,
                dataset_sha256=dataset_sha256,
                run_size=run_size,
            )
        finally:
            heartbeat_stop.set()
            await heartbeat_task
            await self._report_heartbeat("polling")

    async def _evaluate_artifact(
        self,
        agent_id: UUID,
        expected_sha256: str,
        *,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ScoreReport:
        """Fetch, verify, and score one artifact without managing heartbeats."""
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
            progress_callback=progress_callback,
        )
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
        return report

    async def _submit_report(
        self,
        agent_id: UUID,
        miner_hotkey: str,
        report: ScoreReport,
        *,
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        """Sign and submit an already-scored :class:`ScoreReport`. The signature
        binds ``(validator_hotkey, agent_id, ticket_deadline, run_id, composite,
        seed)`` of this exact run. The ticket deadline is the lease identity, so
        a late result cannot be replayed after reissue. Advisory
        ``confirmation_composites`` rides unsigned (like ``composite_stderr``)."""
        if (
            ticket_deadline is not None
            and ticket_deadline.tzinfo is not None
            and ticket_deadline <= datetime.now(UTC)
        ):
            raise PlatformError(
                f"ticket for agent {agent_id} expired before score submission; "
                "leaving it to reopen"
            )
        signature = sign_score(
            self._keypair,
            validator_hotkey=self._config.validator_hotkey,
            agent_id=agent_id,
            ticket_deadline=ticket_deadline,
            run_id=report.run_id,
            composite=report.composite,
            seed=report.seed,
        )
        await self._platform.submit_score(
            agent_id,
            signature=signature,
            report=report,
            ticket_deadline=ticket_deadline,
        )
        logger.info(
            "scored agent %s (miner=%s composite=%.3f seed=%d)",
            agent_id,
            miner_hotkey,
            report.composite,
            report.seed,
        )
        return report

    async def _evaluate_and_submit(
        self,
        agent_id: UUID,
        expected_sha256: str,
        miner_hotkey: str,
        *,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        """Fetch an agent's artifact, score it, sign, and submit. The single-seed
        path used by the ticket sweep (:meth:`_score_job`)."""
        if ticket_deadline is None:
            report = await self._evaluate(
                agent_id,
                expected_sha256,
                seed=seed,
                dataset_sha256=dataset_sha256,
                run_size=run_size,
            )
            return await self._submit_report(agent_id, miner_hotkey, report)

        await self._begin_active_ticket(agent_id, ticket_deadline)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_while_active(heartbeat_stop)
        )
        failure_reported = False
        try:
            report = await self._evaluate_artifact(
                agent_id,
                expected_sha256,
                seed=seed,
                dataset_sha256=dataset_sha256,
                run_size=run_size,
                progress_callback=self._on_dittobench_progress,
            )
            await self._publish_benchmark_progress(
                "finalizing", completed=report.n, total=report.n
            )
            await self._publish_benchmark_progress(
                "submitting_result", completed=report.n, total=report.n
            )
            return await self._submit_report(
                agent_id,
                miner_hotkey,
                report,
                ticket_deadline=ticket_deadline,
            )
        except Exception:
            previous = self._benchmark_progress
            completed = previous.completed if previous is not None else None
            total = previous.total if previous is not None else None
            with contextlib.suppress(Exception):
                failure_reported = await self._publish_benchmark_progress(
                    "failed_retrying", completed=completed, total=total
                )
            if failure_reported:
                self._retain_failed_progress_until = (
                    time.monotonic() + _FAILED_PROGRESS_MIN_VISIBLE_SECONDS
                )
            raise
        finally:
            heartbeat_stop.set()
            await heartbeat_task
            self._clear_active_ticket()
            if not failure_reported:
                await self._report_heartbeat_bounded("polling")

    async def _confirm_and_submit(
        self,
        agent_id: UUID,
        expected_sha256: str,
        miner_hotkey: str,
        *,
        seeds: Sequence[int],
    ) -> ScoreReport | None:
        """P4 re-score of one stale agent over ``seeds`` (K common CRN seeds).

        Evaluates the agent on each seed, then submits a SINGLE signed score: the
        median-composite run (a real run, so its signed composite/seed/run_id are
        genuine), enriched with ``confirmation_composites`` + ``confirmation_seeds``
        = the per-seed composites and their CRN seeds, aligned 1:1 and seed-sorted
        so the fold can pair a later challenger on shared seeds, plus a
        ``composite_stderr`` pooled over those seeds
        (:func:`_pooled_confirmation_stderr`) so the fold's z-band sees the
        between-seed reproducibility, not one run's within-dataset error. The KOTH
        fold then dethrones on the median over seeds
        (:func:`ditto.validator.weights._effective_composite`), so a crown flip
        must replicate across seeds and not ride one lucky common-seed draw, with
        no per-seed rows on the platform. Seeds that fail to score are skipped;
        with one survivor this degrades to the plain single-seed submission and
        with none returns ``None`` (the caller keeps the stale ledger score)."""
        reports: list[ScoreReport] = []
        for s in seeds:
            try:
                reports.append(await self._evaluate(agent_id, expected_sha256, seed=s))
            except (PlatformError, DittobenchError) as exc:
                logger.warning(
                    "re-score of stale agent %s (seed %d) failed; skipping seed: %s",
                    agent_id,
                    s,
                    exc,
                )
        if not reports:
            return None
        # Representative = the middle run by composite (a real run, so the signed
        # composite/seed/run_id stay genuine); ties broken by seed for
        # determinism. With K odd this is the median run; the full per-seed list
        # rides in confirmation_composites so the fold takes the true median.
        ordered = sorted(reports, key=lambda r: (r.composite, r.seed))
        representative = ordered[len(ordered) // 2]
        if len(reports) >= 2:
            # Seed-aligned pairs, sorted by seed for a deterministic wire order,
            # so a later PAIRED dethrone (weights._paired_dethrone) can intersect
            # challenger vs champion on their shared seeds.
            pairs = sorted((r.seed, r.composite) for r in reports)
            seeds = [s for s, _ in pairs]
            composites = [c for _, c in pairs]
            # Report the pooled between-seed SE, not the median run's one-dataset
            # error: the K seeds are already run, so the fold's z-band should see
            # the reproducibility they measure (band tightens ~sqrt(K)).
            representative = representative.model_copy(
                update={
                    "confirmation_composites": composites,
                    "confirmation_seeds": seeds,
                    "composite_stderr": _pooled_confirmation_stderr(
                        composites, representative.composite_stderr
                    ),
                }
            )
        return await self._submit_report(agent_id, miner_hotkey, representative)

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event | None = None,
    ) -> None:
        """Run independent scoring and weight loops until ``stop`` is set.

        A scoring sweep can spend hours on its bounded batch of full benchmark
        runs. Weight cadence therefore cannot be a flag checked before that
        sweep and acted on afterward: doing so starves chain updates whenever
        the queue is busy. The dedicated weight task starts immediately and
        then follows the greater of the configured and on-chain intervals. A
        cooperative updater drain stops both loops from starting new work and
        is acknowledged only after their current work has completed.
        """
        write_update_state("ready", platform_accepted=self._platform_accepted)
        weight_task = asyncio.create_task(
            self._run_weights_forever(stop, drain_requested=drain_requested),
            name="validator-weights",
        )
        try:
            while not stop.is_set():
                if drain_requested is not None and drain_requested.is_set():
                    await self._acknowledge_drain(stop, drain_requested)
                    continue
                try:
                    self._scoring_active = True
                    if drain_requested is None:
                        outcome = await self.run_once(set_weights=False)
                    else:
                        outcome = await self.run_once(
                            set_weights=False,
                            stop_requested=stop,
                            drain_requested=drain_requested,
                        )
                    # Preserve compatibility with lightweight test doubles and
                    # older embedders that still return the historical int.
                    queue_depth = (
                        outcome.queue_depth
                        if isinstance(outcome, _SweepOutcome)
                        else outcome
                    )
                    logger.info("scoring sweep complete: %d agent(s)", queue_depth)
                except Exception:  # noqa: BLE001 - a sweep must never kill the loop
                    logger.exception("scoring sweep failed; retrying next sweep")
                    await self._report_heartbeat("error")
                    # A failed heartbeat may have cleared platform acceptance;
                    # never leave an earlier accepted state on disk.
                    write_update_state(
                        "working", platform_accepted=self._platform_accepted
                    )
                finally:
                    self._scoring_active = False
                await self._sleep_or_stop_or_drain(
                    stop, self._config.sweep_seconds, drain_requested
                )
        finally:
            weight_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await weight_task
            write_update_state("stopping")

    async def _run_weights_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event | None = None,
    ) -> None:
        """Submit weights in a chain-safe window, independently of scoring."""
        chain_floor = await self._chain_min_epoch_seconds()
        has_run_weight_epoch = False
        while not stop.is_set():
            if drain_requested is not None and drain_requested.is_set():
                # The scoring loop is the sole drain-acknowledgement owner: it
                # verifies that this task is inactive before publishing
                # ``drained``. The weight loop only remains quiescent here.
                while drain_requested.is_set() and not stop.is_set():
                    await self._sleep_or_stop(stop, 0.05)
                if not stop.is_set() and has_run_weight_epoch:
                    # A drain interrupts the cadence sleep. Start a fresh
                    # bounded epoch after resume instead of immediately
                    # resubmitting weights and risking the chain rate limit.
                    epoch_seconds = max(float(self._config.epoch_seconds), chain_floor)
                    await self._sleep_or_stop_or_drain(
                        stop, epoch_seconds, drain_requested
                    )
                continue
            epoch_seconds = max(float(self._config.epoch_seconds), chain_floor)
            window_delay = await self._seconds_until_weight_window(epoch_seconds)
            if window_delay > 0:
                logger.info(
                    "weight update is not chain-due; waiting %.0fs before submission",
                    window_delay,
                )
                await self._sleep_or_stop_or_drain(stop, window_delay, drain_requested)
                # Re-read both chain state and drain/stop state after the wait.
                # A commit by another process (or a resumed Pylon task) may have
                # advanced LastUpdate while this worker slept.
                chain_floor = await self._chain_min_epoch_seconds()
                continue
            started = time.monotonic()
            outcome = _WeightOutcome()
            try:
                self._weights_active = True
                # Do not overwrite an active benchmark heartbeat with the
                # short weight state; benchmark progress remains the useful
                # public current-work signal.
                if self._active_agent_id is None:
                    await self._report_heartbeat("updating_weights")
                outcome = await self._update_weights()
                logger.info(
                    "weight epoch complete: pylon_accepted=%s miner(s)=%d",
                    outcome.submitted,
                    len(outcome.weights),
                )
            except Exception:  # noqa: BLE001 - weights retry next epoch
                logger.exception("weight epoch failed; retrying next epoch")
            finally:
                self._weights_active = False
                has_run_weight_epoch = True
            last_update, observed_block = await self._observe_onchain_weight_state()
            self._telemetry.record_sweep(
                SweepStats(
                    sweep_duration_s=time.monotonic() - started,
                    queue_depth=0,
                    failed_count=0 if outcome.submitted else 1,
                    leaderboard=outcome.leaderboard,
                    weights=outcome.weights,
                    weights_submitted=outcome.submitted,
                    weights_due=True,
                    burn_hotkey=self._config.burn_hotkey,
                    onchain_last_update_block=last_update,
                    onchain_observed_block=observed_block,
                    scoring_sweep=False,
                )
            )
            if self._active_agent_id is None:
                await self._report_heartbeat("idle")
            # Re-read the live floor once per epoch so a hyperparameter change
            # is reflected without coupling this task to the scoring loop.
            chain_floor = await self._chain_min_epoch_seconds()
            epoch_seconds = max(float(self._config.epoch_seconds), chain_floor)
            await self._sleep_or_stop_or_drain(stop, epoch_seconds, drain_requested)

    async def _acknowledge_drain(
        self, stop: asyncio.Event, drain_requested: asyncio.Event
    ) -> None:
        """Publish drained only once scoring and weight work are quiescent."""
        while self._weights_active and not stop.is_set():
            await self._sleep_or_stop(stop, 0.05)
        if stop.is_set():
            return
        await self._report_heartbeat("idle")
        write_update_state("drained", platform_accepted=self._platform_accepted)
        await self._wait_for_resume_or_stop(stop, drain_requested)
        if not stop.is_set():
            write_update_state("ready", platform_accepted=self._platform_accepted)

    @staticmethod
    def _new_work_blocked(*events: asyncio.Event | None) -> bool:
        """Whether shutdown/drain has forbidden another unit of work."""
        return any(event is not None and event.is_set() for event in events)

    async def _wait_for_resume_or_stop(
        self, stop: asyncio.Event, drain_requested: asyncio.Event
    ) -> None:
        """Remain quiescent until USR2 resumes work or shutdown is requested."""
        next_bootstrap_heartbeat = 0.0
        while drain_requested.is_set() and not stop.is_set():
            now = time.monotonic()
            if not self._platform_accepted and now >= next_bootstrap_heartbeat:
                await self._report_heartbeat("idle")
                write_update_state("drained", platform_accepted=self._platform_accepted)
                next_bootstrap_heartbeat = now + 5.0
            await ValidatorWorker._sleep_or_stop(stop, 0.05)

    @staticmethod
    async def _sleep_or_stop_or_drain(
        stop: asyncio.Event,
        seconds: float,
        drain_requested: asyncio.Event | None,
    ) -> None:
        """Sleep until cadence, shutdown, or a cooperative drain request."""
        if drain_requested is None:
            await ValidatorWorker._sleep_or_stop(stop, seconds)
            return
        stop_task = asyncio.create_task(stop.wait())
        drain_task = asyncio.create_task(drain_requested.wait())
        try:
            await asyncio.wait(
                {stop_task, drain_task},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_task, drain_task):
                task.cancel()
            await asyncio.gather(stop_task, drain_task, return_exceptions=True)

    async def _chain_min_epoch_seconds(self) -> float:
        """The chain-enforced floor (seconds) on the weight-set cadence.

        Reads the subnet's ``weights_rate_limit`` and ``tempo`` through the
        active weight sink and converts the larger block window to seconds.
        Commit-reveal tasks are tempo-bounded: using only the nominal rate
        limit can enqueue a request that Pylon accepts over HTTP but later
        exhausts its retries with ``CommittingWeightsTooFast``.

        Replaces the hand-set ``VALIDATOR_EPOCH_SECONDS``-only proxy: the loop
        uses ``max(epoch_seconds, this floor)``. **Fail-open:** an unavailable
        rate-limit read returns ``0.0`` so the configured cadence still drives
        the loop. A missing tempo retains the rate-limit floor.
        """
        rate_limit = await self._read_chain_blocks("get_weights_rate_limit")
        if rate_limit is None:
            return 0.0
        tempo = await self._read_chain_blocks("get_tempo")
        cadence_blocks = max(rate_limit, tempo or 0)
        floor = float(cadence_blocks) * _BLOCK_SECONDS
        log = logger.warning if floor > self._config.epoch_seconds else logger.info
        log(
            "chain cadence for netuid %s: weights_rate_limit=%d block(s) "
            "tempo=%s block(s); chain floor=%d block(s) (~%.0fs); "
            "configured epoch_seconds=%d -> "
            "effective %.0fs",
            self._config.netuid,
            rate_limit,
            tempo if tempo is not None else "?",
            cadence_blocks,
            floor,
            self._config.epoch_seconds,
            max(float(self._config.epoch_seconds), floor),
        )
        return floor

    async def _seconds_until_weight_window(self, epoch_seconds: float) -> float:
        """Return a best-effort delay until another commit can be attempted.

        Pylon acknowledges ``put_weights`` before its background task reaches
        Subtensor. On process restart, blindly submitting immediately can race
        the previous successful commit and create a task that only fails later.
        ``LastUpdate`` plus the observed head lets the worker wait out the
        configured/chain cadence first. Evidence reads remain fail-open so a
        temporary Pylon read outage cannot permanently wedge weight liveness.
        """
        last_update, observed_block = await self._observe_onchain_weight_state()
        if last_update is None or observed_block is None:
            return 0.0
        elapsed_blocks = observed_block - last_update
        if elapsed_blocks < 0:
            return 0.0
        required_blocks = math.ceil(epoch_seconds / _BLOCK_SECONDS)
        remaining_blocks = required_blocks - elapsed_blocks
        if remaining_blocks <= 0:
            return 0.0
        # One extra block protects against Pylon's cached head being just behind
        # the node used for the subsequent commit attempt.
        return float(remaining_blocks + 1) * _BLOCK_SECONDS

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

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
import contextvars
import inspect
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ditto.api_models.benchmark_capacity import (
    ActiveBenchmarkSlot,
    BenchmarkAdmission,
    BenchmarkCapacity,
)
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    BenchmarkProgressStage,
)
from ditto.api_models.validator import ValidatorHeartbeatRequest, ValidatorRuntimeState
from ditto.api_models.validator_capabilities import ScorerBenchmarkCapability
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
from ditto.validator.stack_health import fallback_stack_health
from ditto.validator.stack_identity import (
    bind_observed_scorer_identity,
    validator_capabilities_and_stack,
)
from ditto.validator.telemetry import (
    ScoredAgentStat,
    SweepStats,
    TelemetryConfig,
    ValidatorTelemetry,
    scored_agent_stat,
)
from ditto.validator.transform_audit import (
    ALPHA,
    brittleness_pvalue,
    brittleness_signature,
    pool_audit_pairs,
)
from ditto.validator.update_control import write_update_state
from ditto.validator.weights import (
    DEFAULT_BENCH_VERSION,
    _entry_has_seeds,
    agents_needing_rescore,
    apply_miner_emission_cap,
    compute_weights,
    contested_confirmation_set,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.validator import (
        FailJobReason,
        JobResponse,
        LedgerEntry,
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
    from ditto.validator.stack_health import StackHealthCollector

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
_ACTIVE_HEARTBEAT_SECONDS = 30.0
# OpenRouter shortens case latency, so publish aggregate count motion promptly.
# Stage transitions still publish immediately.
_PROGRESS_UPDATE_SECONDS = 15.0
# Active ticket work must never wait on the platform client's normal HTTP timeout.
_ACTIVE_TELEMETRY_TIMEOUT_SECONDS = 2.0
# Keep a successfully reported generic failure visible through at least one
# progress reporting interval. A new ticket supersedes it immediately.
_FAILED_PROGRESS_MIN_VISIBLE_SECONDS = 60.0
_RESOURCE_SLOT_RECOVERY_SECONDS = 10 * 60.0

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


def _attach_transform_audit(
    representative: ScoreReport, reports: Sequence[ScoreReport]
) -> ScoreReport:
    """Record the reproduce-under-transform verdict on the submitted report.

    The platform only ever sees the ONE representative report, so it cannot pool
    the K confirmation runs itself. This sums the audit 2x2 counts across them
    and attaches both the pooled counts and the resulting p-value.

    Pooling is not a refinement, it is what makes a verdict possible at all: a
    single full run yields only a handful of audit pairs and a couple of
    discordant ones, which cannot reach ALPHA however the test is framed.

    The verdict rides ``details``, which is advisory and NOT covered by the
    signature, and never touches the composite. A directional audit result is
    the surface-brittleness signature; it is not evidence about a robust local
    solver, which recomputes correctly under the transform too and was measured
    passing the audit.
    """
    pooled = pool_audit_pairs([r.details for r in reports])
    if (
        pooled["both_correct"]
        + pooled["base_only"]
        + pooled["transform_only"]
        + pooled["both_wrong"]
        == 0
    ):
        return representative  # older scoring engine: nothing measured

    failed = brittleness_signature([r.details for r in reports])
    pvalue = brittleness_pvalue(pooled["base_only"], pooled["transform_only"])
    if failed:
        logger.warning(
            "agent %s: transform-audit brittleness signature — %d base-only vs "
            "%d transform-only discordant pairs over %d run(s), p=%.4f <= %.3f",
            representative.run_id,
            pooled["base_only"],
            pooled["transform_only"],
            len(reports),
            pvalue,
            ALPHA,
        )
    details = dict(representative.details or {})
    details["audit_pairs_pooled"] = pooled
    details["audit_pairs_runs"] = len(reports)
    details["transform_audit_pvalue"] = pvalue
    details["transform_audit_failed"] = failed
    return representative.model_copy(update={"details": details})


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


@dataclass
class _SlotState:
    slot_id: str
    active_agent_id: UUID | None = None
    bench_version: int = DEFAULT_BENCH_VERSION
    ticket_deadline: datetime | None = None
    run_token: str | None = None
    progress: BenchmarkProgress | None = None
    last_progress_heartbeat_monotonic: float | None = None
    last_progress_bucket: int | None = None
    retain_failed_progress_until: float = 0.0


_CURRENT_SLOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "validator_benchmark_slot", default="slot-0"
)


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
        stack_health: StackHealthCollector | None = None,
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
        configured_slots = int(getattr(config, "benchmark_capacity", 1))
        self._slots = {
            f"slot-{index}": _SlotState(slot_id=f"slot-{index}")
            for index in range(configured_slots)
        }
        self._healthy_slots = set(self._slots)
        self._resource_blocked_until: dict[str, float] = {}
        self._admission: BenchmarkAdmission = "accepting"
        # Opaque per-run token for the active ticket, learned from the first
        # scorer snapshot that carries a run id (None for the pre-run stages).
        # Rides every published BenchmarkProgress so the platform can tell a
        # fresh re-attempt apart from the same still-live lease.
        self._active_heartbeat_lock = asyncio.Lock()
        self._system_metrics = system_metrics
        self._stack_health = stack_health

    def _slot_state(self) -> _SlotState:
        return self._slots[_CURRENT_SLOT.get()]

    @property
    def _active_agent_id(self) -> UUID | None:
        return self._slot_state().active_agent_id

    @_active_agent_id.setter
    def _active_agent_id(self, value: UUID | None) -> None:
        self._slot_state().active_agent_id = value

    @property
    def _active_ticket_deadline(self) -> datetime | None:
        return self._slot_state().ticket_deadline

    @_active_ticket_deadline.setter
    def _active_ticket_deadline(self, value: datetime | None) -> None:
        self._slot_state().ticket_deadline = value

    @property
    def _active_run_token(self) -> str | None:
        return self._slot_state().run_token

    @_active_run_token.setter
    def _active_run_token(self, value: str | None) -> None:
        self._slot_state().run_token = value

    @property
    def _benchmark_progress(self) -> BenchmarkProgress | None:
        return self._slot_state().progress

    @_benchmark_progress.setter
    def _benchmark_progress(self, value: BenchmarkProgress | None) -> None:
        self._slot_state().progress = value

    @property
    def _last_progress_heartbeat_monotonic(self) -> float | None:
        return self._slot_state().last_progress_heartbeat_monotonic

    @_last_progress_heartbeat_monotonic.setter
    def _last_progress_heartbeat_monotonic(self, value: float | None) -> None:
        self._slot_state().last_progress_heartbeat_monotonic = value

    @property
    def _last_progress_bucket(self) -> int | None:
        return self._slot_state().last_progress_bucket

    @_last_progress_bucket.setter
    def _last_progress_bucket(self, value: int | None) -> None:
        self._slot_state().last_progress_bucket = value

    @property
    def _retain_failed_progress_until(self) -> float:
        return self._slot_state().retain_failed_progress_until

    @_retain_failed_progress_until.setter
    def _retain_failed_progress_until(self, value: float) -> None:
        self._slot_state().retain_failed_progress_until = value

    def _capacity_snapshot(self) -> BenchmarkCapacity:
        active = []
        for slot in self._slots.values():
            if slot.active_agent_id is None or slot.progress is None:
                continue
            active.append(
                ActiveBenchmarkSlot(
                    slot_id=slot.slot_id,
                    agent_id=slot.active_agent_id,
                    bench_version=slot.bench_version,
                    progress=slot.progress,
                    healthy=slot.slot_id in self._healthy_slots,
                )
            )
        return BenchmarkCapacity(
            configured_slots=len(self._slots),
            healthy_slots=(
                sorted(self._healthy_slots) if self._admission == "accepting" else []
            ),
            admission=self._admission,
            active=active,
        )

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
        self._admission = (
            "draining"
            if drain_requested is not None and drain_requested.is_set()
            else "accepting"
        )
        await self._report_heartbeat("polling")
        write_update_state("working", platform_accepted=self._platform_accepted)
        scored: list[ScoredAgentStat] = []
        failed = 0
        queue_depth = 0
        scoring_available = await self._scoring_preflight()
        if not scoring_available:
            failed = 1
        # Each signed heartbeat slot owns at most one live lease. Sibling slots
        # execute independently: a sandbox/provider failure drains only that slot
        # while healthy siblings continue. The shared counter keeps the sweep's
        # historical queue_limit bound across the whole worker pool.
        if scoring_available:
            budget_lock = asyncio.Lock()
            claimed = 0

            async def run_slot(slot_id: str) -> tuple[list[ScoredAgentStat], int, int]:
                nonlocal claimed
                slot_scored: list[ScoredAgentStat] = []
                slot_failed = 0
                slot_claimed = 0
                token = _CURRENT_SLOT.set(slot_id)
                try:
                    while not self._new_work_blocked(stop_requested, drain_requested):
                        async with budget_lock:
                            if claimed >= self._config.queue_limit:
                                break
                            claimed += 1
                        try:
                            job = await self._platform.request_job(slot_id=slot_id)
                        except PlatformError as error:
                            async with budget_lock:
                                claimed -= 1
                            logger.warning(
                                "job request failed for %s; slot is isolated: %s",
                                slot_id,
                                error,
                            )
                            slot_failed += 1
                            break
                        if job is None:
                            async with budget_lock:
                                claimed -= 1
                            break
                        slot_claimed += 1
                        if job.slot_id != slot_id:
                            await self._report_ticket_failed(job, "infrastructure")
                            slot_failed += 1
                            break
                        if job.deadline <= datetime.now(UTC):
                            logger.warning(
                                "ticket for agent %s already past deadline %s",
                                job.agent_id,
                                job.deadline.isoformat(),
                            )
                            continue
                        try:
                            report = await self._score_job(job)
                            details = (
                                report.details
                                if isinstance(report.details, dict)
                                else {}
                            )
                            slot_scored.append(
                                scored_agent_stat(job.miner_hotkey, report, details)
                            )
                        except ValidatorInfrastructureError as error:
                            logger.warning(
                                "validator infrastructure failed for agent %s "
                                "on %s; sibling slots continue: %s",
                                job.agent_id,
                                slot_id,
                                error,
                            )
                            await self._report_ticket_failed(job, "infrastructure")
                            self._healthy_slots.discard(slot_id)
                            if any(
                                code in str(error)
                                for code in ("sandbox_oom", "sandbox_tmpfs_exhausted")
                            ):
                                self._resource_blocked_until[slot_id] = (
                                    time.monotonic() + _RESOURCE_SLOT_RECOVERY_SECONDS
                                )
                            slot_failed += 1
                            break
                        except (DittobenchError, PlatformError) as error:
                            logger.warning(
                                "scoring agent %s failed on %s: %s",
                                job.agent_id,
                                slot_id,
                                error,
                            )
                            await self._report_ticket_failed(job, "scoring_error")
                            slot_failed += 1
                    return slot_scored, slot_failed, slot_claimed
                finally:
                    _CURRENT_SLOT.reset(token)

            results = await asyncio.gather(
                *(run_slot(slot_id) for slot_id in sorted(self._healthy_slots))
            )
            for slot_scored, slot_failed, slot_claimed in results:
                scored.extend(slot_scored)
                failed += slot_failed
                queue_depth += slot_claimed
        # Score production is platform-lease-bound. In particular, do not infer
        # autonomous re-score work from the public ledger: the score endpoint
        # requires the exact live ticket deadline, and benchmark-version rollout
        # work arrives through request_job() like every other assignment.

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
        scorer_capacity = int(getattr(self._dittobench, "full_run_capacity", 1))
        if scorer_capacity < len(self._slots):
            logger.warning(
                "configured validator capacity %s exceeds scorer capacity %s; "
                "no ticket will be claimed",
                len(self._slots),
                scorer_capacity,
            )
            self._healthy_slots.clear()
            return False
        preflight = getattr(self._dittobench, "preflight", None)
        if preflight is None:
            self._healthy_slots = {
                slot_id
                for slot_id in self._slots
                if self._resource_blocked_until.get(slot_id, 0.0)
                <= time.monotonic()
            }
            return True
        try:
            result = preflight()
            if inspect.isawaitable(result):
                await result
            # A successful trusted scorer probe is the recovery signal for
            # capacity dropped by a prior sibling failure or dependency outage.
            self._healthy_slots = {
                slot_id
                for slot_id in self._slots
                if self._resource_blocked_until.get(slot_id, 0.0)
                <= time.monotonic()
            }
            return True
        except ValidatorInfrastructureError as e:
            self._healthy_slots.clear()
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
        """Serialize all signed heartbeat timestamps and acceptance updates."""
        async with self._active_heartbeat_lock:
            return await self._report_heartbeat_unlocked(
                state, active_snapshot=active_snapshot
            )

    async def _report_heartbeat_unlocked(
        self,
        state: ValidatorRuntimeState,
        *,
        active_snapshot: tuple[UUID | None, BenchmarkProgress | None] | None = None,
    ) -> bool:
        """Best-effort signed build + runtime report; never gate validator work."""
        del active_snapshot  # v10 always signs one atomic all-slot snapshot.
        capacity = self._capacity_snapshot()
        primary = sorted(capacity.active, key=lambda slot: slot.slot_id)
        active_agent_id = primary[0].agent_id if primary else None
        benchmark_progress = primary[0].progress if primary else None
        if primary:
            state = "running_benchmark"
        if (
            self._admission == "accepting"
            and active_agent_id is None
            and any(
                time.monotonic() < slot.retain_failed_progress_until
                for slot in self._slots.values()
            )
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
            capabilities, stack = validator_capabilities_and_stack()
            capability_probe = getattr(
                self._dittobench, "scorer_benchmark_capability", None
            )
            scorer_benchmarks = ScorerBenchmarkCapability(
                status="legacy_v2", supported_bench_versions=(2,)
            )
            if capability_probe is not None:
                observed = capability_probe(stack)
                if inspect.isawaitable(observed):
                    scorer_benchmarks = await observed
            if int(getattr(self._dittobench, "full_run_capacity", 1)) < len(
                self._slots
            ):
                self._healthy_slots.clear()
            # The scorer probe above is authoritative for capacity. Rebuild the
            # signed snapshot after it so a runtime capacity drop is visible in
            # this heartbeat, not one event later.
            capacity = self._capacity_snapshot()
            primary = sorted(capacity.active, key=lambda slot: slot.slot_id)
            active_agent_id = primary[0].agent_id if primary else None
            benchmark_progress = primary[0].progress if primary else None
            if primary:
                state = "running_benchmark"
            stack = bind_observed_scorer_identity(stack, scorer_benchmarks)
            capabilities = capabilities.model_copy(
                update={"scorer_benchmarks": scorer_benchmarks}
            )
            # v9: per-component runtime health. A collector failure (or no
            # collector, as in older wiring and unit-test doubles) degrades to
            # the conservative all-unknown snapshot rather than blocking the
            # heartbeat or inventing observations.
            stack_health = None
            if self._stack_health is not None:
                try:
                    stack_health = await self._stack_health.collect(
                        stack=stack, scorer=scorer_benchmarks
                    )
                except Exception as probe_error:  # noqa: BLE001 - never gate work
                    logger.warning(
                        "stack-health collection failed; reporting unknown: %s",
                        probe_error,
                    )
            if stack_health is None:
                stack_health = fallback_stack_health()
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
                capabilities=capabilities,
                stack=stack,
                stack_health=stack_health,
                benchmark_capacity=capacity,
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
                capabilities=capabilities,
                stack=stack,
                stack_health=stack_health,
                benchmark_capacity=capacity,
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
            try:
                delivered = await asyncio.wait_for(
                    self._report_heartbeat_unlocked(
                        "running_benchmark", active_snapshot=active_snapshot
                    ),
                    timeout=_ACTIVE_TELEMETRY_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "validator progress heartbeat timed out; scoring continues"
                )
                delivered = False
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
                run_token=self._active_run_token,
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
        # Learn the run identity the moment the scorer first reports it; from
        # here on every progress heartbeat for this ticket carries the token.
        if snapshot.run_token is not None:
            self._active_run_token = snapshot.run_token
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
        self,
        agent_id: UUID,
        ticket_deadline: datetime,
        bench_version: int = DEFAULT_BENCH_VERSION,
    ) -> None:
        """Reset progress throttling and publish artifact preparation promptly."""
        self._retain_failed_progress_until = 0.0
        self._active_agent_id = agent_id
        self._active_ticket_deadline = ticket_deadline
        self._slot_state().bench_version = bench_version
        self._active_run_token = None
        self._benchmark_progress = None
        self._last_progress_heartbeat_monotonic = None
        self._last_progress_bucket = None
        await self._publish_benchmark_progress("preparing")

    def _clear_active_ticket(self) -> None:
        self._active_agent_id = None
        self._active_ticket_deadline = None
        self._active_run_token = None
        self._benchmark_progress = None
        self._last_progress_heartbeat_monotonic = None
        self._last_progress_bucket = None
        self._slot_state().bench_version = DEFAULT_BENCH_VERSION

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

        # The platform serves a last-known-good ledger (flagged stale) when its own
        # DB read fails; folding it is safe (the pool is durable + slow-moving) but
        # worth a loud line so an operator sees the platform is degraded.
        if getattr(ledger, "stale", False):
            logger.warning(
                "scoring ledger is STALE (platform served a %ss-old snapshot); "
                "folding it but the platform DB read is failing",
                getattr(ledger, "age_seconds", "?"),
            )

        # Platform history is intentionally durable across chain deregistration,
        # but only hotkeys that currently have a neuron may participate in the
        # KOTH fold. Pylon also drops missing hotkeys, but doing that *after*
        # champion/tail selection lets an absent miner occupy a paid slot and
        # changes the normalized miner/burn ratio. Filter before the fold so the
        # next registered contender receives the correct role and share.
        registered_entries = await self._registered_ledger_entries(ledger.entries)
        if registered_entries is None:
            # Eligibility is a live-chain fact. On an indeterminate read, leave
            # the last accepted vector untouched instead of either paying an
            # absent hotkey or replacing the vector with 100% burn.
            return _WeightOutcome(
                leaderboard=[(e.miner_hotkey, e.composite) for e in ledger.entries]
            )

        # Version-rollout re-scores are ordinary platform-leased jobs. The fold
        # reads whatever leased scorers have persisted, and compute_weights
        # ignores stale versions defensively regardless.
        leaderboard = [(e.miner_hotkey, e.composite) for e in ledger.entries]
        miner_weights = compute_weights(
            registered_entries,
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
        self, entries: Sequence[LedgerEntry]
    ) -> list[LedgerEntry] | None:
        """Keep only miners currently registered on this subnet.

        The platform remains the source of durable submissions, screening
        history, and accepted scores. The metagraph is only an epoch-local
        payout-eligibility gate: re-registering the same hotkey automatically
        restores its existing ledger entry, while a different hotkey cannot
        inherit it because matching is by the exact SS58 address.

        ``None`` means the chain read failed and the caller must leave weights
        unchanged. A non-awaitable reader is accepted only for lightweight test
        doubles that predate this method; the production ``ChainClient`` always
        returns an awaitable.
        """
        if self._chain is None:
            logger.warning(
                "cannot resolve miner registration without a chain client; "
                "weights unchanged this epoch"
            )
            return None
        read = getattr(self._chain, "get_recent_neurons", None)
        if read is None:
            logger.warning(
                "chain client has no metagraph reader; weights unchanged this epoch"
            )
            return None
        try:
            result = read(self._config.netuid)
            if not inspect.isawaitable(result):
                # Existing unit-test fakes historically model only put_weights.
                # Real ChainClient.get_recent_neurons is always asynchronous.
                return list(entries)
            neurons = await result
        except Exception as e:  # noqa: BLE001 - every read failure is fail-closed
            logger.warning(
                "miner registration read failed; weights unchanged this epoch: %s",
                e,
            )
            return None

        registered = {neuron.hotkey for neuron in neurons}
        kept = [entry for entry in entries if entry.miner_hotkey in registered]
        absent = sorted({entry.miner_hotkey for entry in entries} - registered)
        if absent:
            logger.info(
                "excluding %d deregistered miner hotkey(s) from this epoch's "
                "weight fold: %s",
                len(absent),
                absent,
            )
        return kept

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
        ledger = await self._rescore_stale_champion_and_tail(
            ledger,
            stop_requested=stop_requested,
            drain_requested=drain_requested,
        )
        await self._confirm_contested_dethrone(
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

    async def _confirm_contested_dethrone(
        self,
        ledger: LedgerResponse,
        *,
        stop_requested: asyncio.Event | None = None,
        drain_requested: asyncio.Event | None = None,
    ) -> None:
        """Settle a within-band crown contest on the champion-anchored CRN seeds.

        When a current-version challenger's effective composite sits inside the
        unpaired indifference band of the champion, the crown decision is
        inside seed-luck range: the champion's confirmation composites are a
        frozen draw and the challenger holds one commit-reveal seed, so
        neither side's dataset difficulty cancels. Re-score the champion and
        each unsettled in-band challenger
        (:func:`ditto.validator.weights.contested_confirmation_set`) on a
        common seed set derived from the CHAMPION's agent id alone, so the
        fold's next read decides on the PAIRED statistic
        (weights._paired_dethrone), which cancels per-seed difficulty.

        Anchoring the seeds to the champion (not the contested cohort) is what
        bounds the work: the seed set does not move when a new challenger
        appears, so already-settled challengers keep sharing the champion's
        seeds and are never re-scored, and the champion is re-scored only until
        it carries those seeds once. A newly appearing challenger costs one
        confirmation, not a re-run of the whole cohort. Clear wins and clear
        losses never trigger this. One member failing to re-score is logged
        and its ledger score stands.
        """
        contested = contested_confirmation_set(
            ledger.entries,
            current_version=self._current_bench_version,
            margin=self._config.koth_margin,
            dethrone_z=self._config.koth_dethrone_z,
        )
        if not contested:
            return
        champion = contested[0]
        challengers = contested[1:]
        # Champion-anchored: a pure function of the champion's identity and the
        # version, so it is stable across sweeps and identical fleet-wide.
        seeds = confirmation_seeds(
            [str(champion.agent_id)],
            version=self._current_bench_version,
            count=self._config.koth_confirmation_seeds,
        )
        logger.info(
            "contested dethrone: %d challenger(s) inside champion %s's band; "
            "confirming on champion-anchored CRN seeds %s",
            len(challengers),
            champion.agent_id,
            seeds,
        )
        # Score the champion once, only until its entry already carries the
        # anchored seeds (a later sweep with a fresh challenger must not
        # re-run the champion).
        to_score = list(challengers)
        if not _entry_has_seeds(champion, seeds):
            to_score.insert(0, champion)
        for e in to_score:
            if self._new_work_blocked(stop_requested, drain_requested):
                return
            submitted = await self._confirm_and_submit(
                e.agent_id, e.sha256, e.miner_hotkey, seeds=seeds
            )
            if submitted is None:
                logger.warning(
                    "contested-dethrone confirmation of agent %s produced no "
                    "score; leaving its ledger score",
                    e.agent_id,
                )

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

    async def _report_ticket_failed(
        self, job: JobResponse, reason: FailJobReason
    ) -> None:
        """Best-effort hand-back of a failed ticket for immediate reissue.

        Closing the live lease lets the next :meth:`request_job` mint a fresh
        ticket instead of resuming the failed attempt. Strictly best-effort: an
        old platform without ``/validator/job/fail``, or any transport/validation
        error, must never crash the sweep — the ticket then simply expires on its
        own deadline exactly as it did before this endpoint existed.
        """
        try:
            await self._platform.report_ticket_failed(job, reason)
        except Exception as e:  # noqa: BLE001 - hand-back is best-effort telemetry
            logger.warning(
                "handing back failed ticket for agent %s did not land "
                "(ticket will expire on its own): %s",
                job.agent_id,
                e,
            )

    async def _score_job(self, job: JobResponse) -> ScoreReport:
        """Score one issued ticket against its platform-pinned dataset.

        When the ticket pins the seed's on-chain block hash, the seed is
        re-derived locally first (prod hardening P2): a mismatch means the
        platform issued a seed it could have chosen — refuse to score rather
        than lend the ticket a signature. Tickets without a block hash
        (pre-derivation agents) proceed as before.
        """
        if (
            job.bench_version is not None
            and job.bench_version >= 3
            and (
                job.minimum_screening_policy_version != 9
                or job.requires_screened_image is not True
            )
        ):
            raise PlatformError(
                f"benchmark v{job.bench_version} ticket did not declare its "
                "policy-9 screened-image contract"
            )
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
        inference_session_id: str | None = None
        if (
            job.inference is None
            and getattr(self._config, "inference_proxy_required", False) is True
        ):
            raise ValidatorInfrastructureError(
                "platform inference is required but the ticket carried no capability"
            )
        if job.inference is not None:
            broker = await self._dittobench.prepare_inference_session()
            try:
                exchange = await self._platform.exchange_inference_grant(
                    job.inference.grant_id,
                    broker.broker_public_key,
                    job.inference.exchange_url,
                )
                if (
                    exchange.grant_id != job.inference.grant_id
                    or exchange.proxy_url != job.inference.proxy_url
                    or exchange.expires_at > job.inference.expires_at
                    or exchange.expires_at > job.deadline
                ):
                    raise PlatformError("inference exchange escaped ticket bounds")
                await self._dittobench.activate_inference_session(
                    broker,
                    grant_id=exchange.grant_id,
                    bearer=exchange.bearer,
                    proxy_url=exchange.proxy_url,
                    generation=exchange.generation,
                    expires_at=exchange.expires_at,
                )
            except BaseException:
                await self._dittobench.cancel_inference_session(broker.session_id)
                raise
            inference_session_id = broker.session_id
        try:
            return await self._evaluate_and_submit(
                job.agent_id,
                job.sha256,
                job.miner_hotkey,
                seed=job.seed,
                dataset_sha256=job.dataset_sha256,
                run_size=job.run_size,
                bench_version=job.bench_version,
                ticket_deadline=job.deadline,
                inference_session_id=inference_session_id,
            )
        finally:
            if inference_session_id is not None:
                await self._dittobench.cancel_inference_session(inference_session_id)

    async def _evaluate(
        self,
        agent_id: UUID,
        expected_sha256: str,
        *,
        seed: int | None = None,
        dataset_sha256: str | None = None,
        run_size: str | None = None,
        bench_version: int | None = None,
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
                bench_version=bench_version,
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
        bench_version: int | None = None,
        progress_callback: ProgressCallback | None = None,
        inference_session_id: str | None = None,
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
        if artifact.bench_version != bench_version:
            raise PlatformError(
                f"benchmark version mismatch for agent {agent_id}: "
                f"ticket={bench_version!r} artifact={artifact.bench_version!r}"
            )
        if (
            bench_version is not None
            and bench_version >= 3
            and (
                artifact.screening_policy_version is None
                or artifact.screening_policy_version < 9
                or artifact.screened_image_url is None
            )
        ):
            raise PlatformError(
                f"benchmark v{bench_version} artifact for agent {agent_id} is "
                "not backed by screening policy 9 and a verified image"
            )
        report = await self._dittobench.score_tarball(
            tarball_url=artifact.download_url,
            tarball_sha256=artifact.sha256,
            seed=seed,
            dataset_sha256=dataset_sha256,
            run_size=run_size,
            bench_version=bench_version,
            progress_callback=progress_callback,
            screened_image_url=artifact.screened_image_url,
            screened_image_sha256=artifact.screened_image_sha256,
            screened_image_size_bytes=artifact.screened_image_size_bytes,
            screened_image_id=artifact.screened_image_id,
            screened_image_ref=artifact.screened_image_ref,
            inference_session_id=inference_session_id,
        )
        details = report.details
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
        # Offline reproducibility: a transcript digest in the report details is
        # bound into the signature, so the artifact published below cannot be
        # swapped after the fact. Reports without one keep the legacy payload.
        transcript_sha256 = (
            report.details.get("transcript_sha256")
            if isinstance(report.details, dict)
            else None
        )
        if not isinstance(transcript_sha256, str) or not transcript_sha256:
            transcript_sha256 = None
        signature = sign_score(
            self._keypair,
            validator_hotkey=self._config.validator_hotkey,
            agent_id=agent_id,
            ticket_deadline=ticket_deadline,
            run_id=report.run_id,
            composite=report.composite,
            seed=report.seed,
            bench_version=report.bench_version,
            transcript_sha256=transcript_sha256,
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
        await self._publish_transcript(agent_id, report, transcript_sha256)
        return report

    async def _publish_transcript(
        self, agent_id: UUID, report: ScoreReport, transcript_sha256: str | None
    ) -> None:
        """Best-effort publication of the signed score's transcript artifact.

        The digest is already inside the accepted, signed score; the platform
        verifies the bytes hash to it before storing them content-addressed.
        Failure logs and never unwinds the score — the artifact can be
        re-published, the score cannot be lost."""
        if transcript_sha256 is None:
            return
        take_transcript = getattr(self._dittobench, "take_transcript", None)
        transcript = (
            take_transcript(report.run_id) if callable(take_transcript) else None
        )
        if not isinstance(transcript, bytes) or not transcript:
            logger.warning(
                "agent %s declared transcript %s but no bytes are held; "
                "skipping publication",
                agent_id,
                transcript_sha256,
            )
            return
        try:
            await self._platform.submit_transcript(
                agent_id, run_id=report.run_id, body=transcript
            )
            logger.info(
                "published transcript for agent %s (run=%s sha256=%s bytes=%d)",
                agent_id,
                report.run_id,
                transcript_sha256,
                len(transcript),
            )
        except PlatformError as e:
            logger.warning(
                "transcript publication failed for agent %s: %s", agent_id, e
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
        bench_version: int | None = None,
        ticket_deadline: datetime | None = None,
        inference_session_id: str | None = None,
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
                bench_version=bench_version,
            )
            return await self._submit_report(agent_id, miner_hotkey, report)

        await self._begin_active_ticket(
            agent_id, ticket_deadline, bench_version or DEFAULT_BENCH_VERSION
        )
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
                bench_version=bench_version,
                progress_callback=self._on_dittobench_progress,
                inference_session_id=inference_session_id,
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
                reports.append(
                    await self._evaluate(
                        agent_id,
                        expected_sha256,
                        seed=s,
                        bench_version=self._current_bench_version,
                    )
                )
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
        representative = _attach_transform_audit(representative, reports)
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
        self._admission = "draining"
        await self._report_heartbeat("idle")
        write_update_state("drained", platform_accepted=self._platform_accepted)
        await self._wait_for_resume_or_stop(stop, drain_requested)
        if not stop.is_set():
            self._admission = "accepting"
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

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
from ditto.api_models.validator import (
    ConfirmationDatasetPin,
    ValidatorHeartbeatRequest,
    ValidatorHeartbeatResponse,
    ValidatorRuntimeState,
)
from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ScorerLivenessProbe,
)
from ditto.chain import ChainError
from ditto.validator.build_info import validator_build_info
from ditto.validator.config import lease_budget_seconds
from ditto.validator.crn import confirmation_seeds
from ditto.validator.errors import (
    DittobenchError,
    LeaseDeadlineError,
    LeaseRevokedError,
    PlatformError,
    PlatformInfrastructureError,
    SandboxOomError,
    ValidatorInfrastructureError,
    WeightSubmissionError,
    failure_detail,
)
from ditto.validator.lease_roster import (
    RosterUnknown,
    plan_cancellations,
    read_roster,
)
from ditto.validator.onchain_seed import seed_matches
from ditto.validator.resource_gate import (
    DEFAULT_RESOURCE_CEILINGS,
    ConstrainedResource,
    ResourceCeilings,
)
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
    Top5ConfirmationPlan,
    _entry_has_seeds,
    agents_needing_rescore,
    apply_miner_emission_cap,
    compute_weights,
    contested_confirmation_set,
    select_champion,
    top5_confirmation_set,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.system_health import SystemMetrics
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
        InferenceBrokerSession,
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
_ACTIVE_HEARTBEAT_SECONDS = 10.0
# OpenRouter shortens case latency, so publish aggregate count motion promptly.
# Stage transitions still publish immediately.
_PROGRESS_UPDATE_SECONDS = 5.0
# Active ticket work must never wait on the platform client's normal HTTP timeout.
_ACTIVE_TELEMETRY_TIMEOUT_SECONDS = 2.0
# The caller stops waiting after the short budget above, but that must not cancel
# a signed snapshot already on its way to the platform.  Capability probes and a
# burst of sibling slots can legitimately take longer than two seconds.  Keep
# the send alive in the background under this separate hard bound so telemetry
# remains fail-open without leaking a hung task forever.
_ACTIVE_TELEMETRY_HARD_TIMEOUT_SECONDS = 30.0
# Hard bound on the signed ticket hand-back. It is reached from the lease-abort
# path with only ``LEASE_REPORT_MARGIN_SECONDS`` (120s) left, and it shares that
# margin with the scorer-run cancellation (``_CANCEL_TIMEOUT_SECONDS``, 15s):
# 15 + 30 leaves ~75s of headroom, so the two together cannot exhaust it. There
# is nothing to gain from sizing it above the HTTP client's own 30s
# per-request default, and the platform rejects a signed validator request
# older than two minutes, so a report prepared at the abort point has to land
# well inside that window regardless.
_FAIL_REPORT_TIMEOUT_SECONDS = 30.0
# Keep a successfully reported generic failure visible through at least one
# progress reporting interval. A new ticket supersedes it immediately.
_FAILED_PROGRESS_MIN_VISIBLE_SECONDS = 60.0
_RESOURCE_SLOT_RECOVERY_SECONDS = 10 * 60.0
# How long a slot that found an empty queue waits before polling again, while a
# sibling slot is still executing a lease. It only bounds how quickly free
# capacity notices that the queue refilled; the platform's cap, not this, decides
# how many slots actually receive tickets. Short enough that a quorum opening is
# picked up promptly, long enough that seven idle slots are not a poll storm.
_IDLE_SLOT_REPOLL_SECONDS = 15.0

# Must stay exhaustive over ``BenchmarkProgressStage``: a missing stage raises
# KeyError in ``_publish_benchmark_progress``, which the fail-open telemetry
# handler swallows, silently dropping every update for that stage.
_PROGRESS_STAGE_ORDER: dict[BenchmarkProgressStage, int] = {
    "preparing": 0,
    "building_harness": 1,
    "generating_dataset": 2,
    "starting_harness": 3,
    "running_benchmark": 4,
    # A relay pause is a reversible sub-state of the running stage. Giving both
    # the same rank permits waiting -> running without weakening monotonicity.
    "waiting_for_relay": 4,
    "finalizing": 5,
    "submitting_result": 6,
    "failed_retrying": 7,
}


class _HeartbeatClock:
    """Injectable wall clock for deterministic heartbeat rate-limit tests."""

    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _new_heartbeat_clock() -> _HeartbeatClock:
    return _HeartbeatClock()


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
    king_fingerprint: tuple[str, UUID, float, int | None] | None = None


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
    # Set when the platform's heartbeat roster stops listing this slot's lease.
    # Only ever *requests* a stop: the scoring path owns the unwind, so the
    # scorer-side container kill and the slot reset keep their single home.
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


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
        heartbeat_clock: _HeartbeatClock | None = None,
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
        self._heartbeat_clock = heartbeat_clock or _new_heartbeat_clock()
        self._pending_heartbeat_state: ValidatorRuntimeState | None = None
        self._pending_heartbeat_progress: dict[str, BenchmarkProgress] = {}
        self._coalesced_heartbeat_task: asyncio.Task[bool] | None = None
        self._background_heartbeat_tasks: set[asyncio.Task[bool]] = set()
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
        # Host-resource self-gate. A ``MagicMock`` config (the unit-test double)
        # would otherwise hand us a mock in place of the ceilings and every
        # comparison below would be meaningless, so accept only the real type
        # and fall back to the shipped defaults.
        configured_ceilings = getattr(config, "resource_ceilings", None)
        self._resource_ceilings: ResourceCeilings = (
            configured_ceilings
            if isinstance(configured_ceilings, ResourceCeilings)
            else DEFAULT_RESOURCE_CEILINGS
        )
        self._constrained_resources: tuple[ConstrainedResource, ...] = ()
        # Opaque per-run token for the active ticket, learned from the first
        # scorer snapshot that carries a run id (None for the pre-run stages).
        # Rides every published BenchmarkProgress so the platform can tell a
        # fresh re-attempt apart from the same still-live lease.
        self._active_heartbeat_lock = asyncio.Lock()
        self._system_metrics = system_metrics
        self._stack_health = stack_health
        # A locally persisted score can change the king immediately. The weight
        # loop also polls for receipts from other validators, but this event
        # removes the local sweep-delay without weakening chain cadence.
        self._ledger_changed = asyncio.Event()

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

    def _collect_system_metrics(self) -> SystemMetrics | None:
        """Return the cached coarse host sample, or ``None`` if unobservable.

        The collector already caches on its own reporting cadence, so calling
        this per sweep costs nothing beyond the cache read. A collector failure
        is reported as "no observation" rather than as pressure: refusing work
        because psutil hiccuped would be the opposite of protective.
        """
        if self._system_metrics is None:
            return None
        try:
            return self._system_metrics.collect()
        except Exception as e:  # noqa: BLE001 - telemetry must never gate work
            logger.warning(
                "host metric collection failed; the resource gate stays open: %s", e
            )
            return None

    def _refresh_resource_admission(self, metrics: SystemMetrics | None) -> None:
        """Re-evaluate the self-gate and set ``admission`` from one sample.

        Drain and operator pause outrank a resource decline: both are stronger,
        deliberate statements about this worker, and neither should be
        downgraded to "the disk is a bit full" in the fleet view.

        The transition is logged, not the steady state, so a host that sits
        constrained for an hour produces two lines rather than one per sweep.
        """
        if self._admission in ("draining", "paused"):
            return
        exceeded = self._resource_ceilings.exceeded(metrics)
        if exceeded != self._constrained_resources:
            if exceeded:
                logger.warning(
                    "host is resource constrained (%s); declining to claim new "
                    "tickets until it recovers. Active benchmarks continue and "
                    "heartbeats keep reporting, so this is visibly idle-by-"
                    "choice rather than silently absent.",
                    self._resource_ceilings.describe(metrics),
                )
            else:
                logger.info(
                    "host resources recovered (%s); claiming tickets again",
                    self._resource_ceilings.describe(metrics),
                )
            self._constrained_resources = exceeded
        self._admission = "resource_constrained" if exceeded else "accepting"

    def _capacity_snapshot(self) -> BenchmarkCapacity:
        active = []
        for slot in self._slots.values():
            if slot.active_agent_id is None:
                continue
            # `progress is None` is NOT a reason to omit the slot. A leased slot
            # with nothing to report yet is occupied, and the platform cannot
            # tell an omitted slot from a free one -- which is how a live lease
            # got revoked mid-run. Report the claim; the progress catches up.
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
        # Decide before the first heartbeat of the sweep, so the snapshot the
        # platform receives already says why this validator is about to sit out.
        self._refresh_resource_admission(self._collect_system_metrics())
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
        #
        # ``running`` counts the slots currently executing a lease. It is what
        # lets an empty poll tell "this sweep is finished" apart from "the queue
        # had nothing for me *this second*" -- see the ``job is None`` branch in
        # ``run_slot``, which must not retire a slot for the rest of the sweep.
        if scoring_available:
            budget_lock = asyncio.Lock()
            claimed = 0
            running = 0
            pending_claims = 0
            # Set whenever a claim resolves or a slot finishes a lease, so a
            # waiting slot re-polls the instant the pool changes instead of
            # sitting out the interval. Cleared and read under ``budget_lock``
            # together with ``running`` and ``pending_claims``, which makes the
            # wakeup impossible to miss.
            lease_state_changed = asyncio.Event()
            # Only one idle slot needs to fan out the host-level continual
            # retest lane.  Platform claims remain slot-bound, so that one
            # dispatcher can fill every currently free slot while ordinary
            # sibling leases continue running.  Other idle slot loops keep
            # polling the canonical queue instead of lining up behind it.
            idle_retest_dispatch = asyncio.Lock()

            async def run_slot(slot_id: str) -> tuple[list[ScoredAgentStat], int, int]:
                nonlocal claimed, pending_claims, running
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
                            pending_claims += 1
                        try:
                            job = await self._platform.request_job(slot_id=slot_id)
                        except PlatformError as error:
                            async with budget_lock:
                                claimed -= 1
                                pending_claims -= 1
                                lease_state_changed.set()
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
                                pending_claims -= 1
                                siblings_running = bool(running)
                                sibling_work_possible = bool(
                                    siblings_running or pending_claims
                                )
                                if sibling_work_possible:
                                    lease_state_changed.clear()
                                else:
                                    # Wake empty siblings that were waiting for
                                    # this last outstanding claim to settle.
                                    lease_state_changed.set()
                            if not sibling_work_possible:
                                # Nothing of this worker's is in flight, so an
                                # empty poll really is an empty queue. End the
                                # sweep and let the ordinary sweep cadence bring
                                # the whole pool back at once.
                                break
                            # Canonical work was checked first for this slot.
                            # Use the otherwise-idle gap for continual retests
                            # now, rather than waiting for every sibling's
                            # (potentially ninety-minute) ordinary lease to
                            # finish before reaching the post-gather lane.
                            #
                            # The lane itself asks the platform for durable,
                            # per-slot leases.  A single host-level dispatcher
                            # is therefore enough to fan out across all free
                            # slots without duplicate local fanouts.
                            if siblings_running and not idle_retest_dispatch.locked():
                                async with idle_retest_dispatch:
                                    await self._run_top5_confirmation_lane(
                                        stop_requested=stop_requested,
                                        drain_requested=drain_requested,
                                    )
                            # A sibling still holds a lease, and ``asyncio.gather``
                            # below does not return until it does -- up to the
                            # full ninety-minute lease. Breaking here would retire
                            # this slot for that entire time, and the sibling's own
                            # loop keeps re-claiming, so the sweep never ends and
                            # the slots that lost the first poll never poll again.
                            # That is how a host advertising eight slots serves
                            # exactly one benchmark no matter how high the
                            # platform's cap is set. The queue refills constantly
                            # (quorum openings, expiries, new submissions), so wait
                            # and ask again instead of leaving the sweep.
                            await self._sleep_or_interrupt(
                                _IDLE_SLOT_REPOLL_SECONDS,
                                stop_requested,
                                drain_requested,
                                lease_state_changed,
                            )
                            continue
                        async with budget_lock:
                            pending_claims -= 1
                            lease_state_changed.set()
                        slot_claimed += 1
                        if job.slot_id != slot_id:
                            await self._report_ticket_failed(
                                job, "infrastructure", "ticket_slot_mismatch"
                            )
                            slot_failed += 1
                            break
                        if job.deadline <= datetime.now(UTC):
                            logger.warning(
                                "ticket for agent %s already past deadline %s",
                                job.agent_id,
                                job.deadline.isoformat(),
                            )
                            continue
                        # From here to the ``finally`` this slot is executing a
                        # lease, which is exactly the window that keeps an idle
                        # sibling waiting rather than abandoning the sweep.
                        async with budget_lock:
                            running += 1
                        try:
                            report = await self._score_job_within_lease(job)
                            self._ledger_changed.set()
                            details = (
                                report.details
                                if isinstance(report.details, dict)
                                else {}
                            )
                            slot_scored.append(
                                scored_agent_stat(job.miner_hotkey, report, details)
                            )
                        except LeaseRevokedError as error:
                            # Nothing is reported back, and that is the point.
                            # The platform revoked this lease itself, so there
                            # is no ticket left to hand back: `scoring_error`
                            # would consume an attempt the platform already took
                            # away, and `infrastructure` would mint a no-fault
                            # grant and re-lease the submission forever. Silence
                            # here is the correct wire behaviour.
                            #
                            # Listed first, and its own exception hierarchy, so
                            # it can never be reordered into the DittobenchError
                            # branch below the way LeaseDeadlineError can.
                            logger.warning(
                                "lease for agent %s on %s was revoked by the "
                                "platform; slot freed immediately instead of "
                                "at the lease TTL: %s",
                                job.agent_id,
                                slot_id,
                                error,
                            )
                            # The scoring path may have been cut before its own
                            # cleanup ran, so stop advertising a lease this
                            # worker no longer holds.
                            self._clear_active_ticket()
                            slot_failed += 1
                        except LeaseDeadlineError as error:
                            # Reported as scoring_error, never infrastructure.
                            # See LeaseDeadlineError: an artifact that consumed
                            # its whole lease without a verdict must consume the
                            # attempt, or the platform's no-fault infra grant
                            # re-leases it forever and it never resolves.
                            logger.warning(
                                "lease deadline reached for agent %s on %s "
                                "(deadline=%s); handing the ticket back as "
                                "scoring_error rather than letting it expire: "
                                "%s",
                                job.agent_id,
                                slot_id,
                                job.deadline.isoformat(),
                                error,
                            )
                            await self._report_ticket_failed(
                                job, "scoring_error", failure_detail(error)
                            )
                            slot_failed += 1
                        except SandboxOomError as error:
                            logger.warning(
                                "sandbox out of memory for agent %s on %s; "
                                "deferring harness and continuing: %s",
                                job.agent_id,
                                slot_id,
                                error,
                            )
                            await self._report_ticket_failed(
                                job, "sandbox_oom", failure_detail(error)
                            )
                            slot_failed += 1
                        except (
                            ValidatorInfrastructureError,
                            PlatformInfrastructureError,
                        ) as error:
                            logger.warning(
                                "validator infrastructure failed for agent %s "
                                "on %s; sibling slots continue: %s",
                                job.agent_id,
                                slot_id,
                                error,
                            )
                            await self._report_ticket_failed(
                                job, "infrastructure", failure_detail(error)
                            )
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
                            await self._report_ticket_failed(
                                job, "scoring_error", failure_detail(error)
                            )
                            slot_failed += 1
                        finally:
                            async with budget_lock:
                                running -= 1
                                lease_state_changed.set()
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
        # requires the exact live ticket deadline. The only autonomous-looking
        # follow-up below is also platform-leased through the dedicated top-five
        # claim endpoint and appends evidence without replacing canonical scores.
        # Continual confirmation is strictly spare-capacity work: every healthy
        # slot above has polled the ordinary queue empty and all sibling leases
        # have finished before the gather returns. ``queue_depth`` is only the
        # historical claim count for this sweep. Gating on it made one completed
        # ordinary job suppress every idle retest slot until a later sweep.
        # Spare-capacity work is still work: a constrained host must not claim
        # a confirmation ticket either, so gate the lane on admission directly
        # rather than relying on the (now empty) healthy-slot set.
        if (
            scoring_available
            and self._admission == "accepting"
            and not self._new_work_blocked(stop_requested, drain_requested)
        ):
            await self._run_top5_confirmation_lane(
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

    def _available_slots(self) -> set[str]:
        """Slots this sweep may claim on: unblocked, and within scorer capacity.

        The scorer's advertised full-run capacity is a hard ceiling -- claiming
        past it only earns a 429 from a saturated run-slot channel. The two
        values ride one Compose variable, so they agree on a stack that was
        recreated together; they can disagree only while a targeted update has
        refreshed the worker but not yet the scorer.

        That window is why this narrows instead of refusing. Both are true
        bounds, so the smaller one is the safe answer, and a worker that
        advertises more slots than its scorer can serve degrades to the scorer's
        number rather than stopping. Refusing outright would turn a transient
        update ordering into a validator that scores nothing at all, and a
        three-validator quorum cannot spare one.
        """
        # A host past its own resource ceiling offers nothing this sweep. It
        # keeps heartbeating (with ``admission="resource_constrained"``), and
        # any slot already running a benchmark stays in ``capacity.active``, so
        # a live lease is never mistaken for an abandoned one.
        if self._admission != "accepting":
            return set()
        scorer_capacity = int(getattr(self._dittobench, "full_run_capacity", 1))
        unblocked = sorted(
            slot_id
            for slot_id in self._slots
            if self._resource_blocked_until.get(slot_id, 0.0) <= time.monotonic()
        )
        if scorer_capacity < len(self._slots):
            logger.warning(
                "configured validator capacity %s exceeds scorer capacity %s; "
                "running the scorer's %s slot(s) until the stack agrees",
                len(self._slots),
                scorer_capacity,
                scorer_capacity,
            )
        return set(unblocked[:scorer_capacity])

    async def _scoring_preflight(self) -> bool:
        """Functionally probe scorer dependencies before requesting a lease."""
        preflight = getattr(self._dittobench, "preflight", None)
        if preflight is None:
            self._healthy_slots = self._available_slots()
            return True
        try:
            result = preflight()
            if inspect.isawaitable(result):
                await result
            # A successful trusted scorer probe is the recovery signal for
            # capacity dropped by a prior sibling failure or dependency outage.
            self._healthy_slots = self._available_slots()
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
        """Coalesce callers and send at most once per wall-clock second."""
        slot_id = _CURRENT_SLOT.get()
        sent_progress = active_snapshot[1] if active_snapshot is not None else None
        async with self._active_heartbeat_lock:
            if (
                self._coalesced_heartbeat_task is None
                and int(self._heartbeat_clock.time()) > self._last_heartbeat_timestamp
            ):
                delivered = await self._report_heartbeat_unlocked(state)
                if delivered and sent_progress is not None:
                    self._record_delivered_progress(slot_id, sent_progress)
                return delivered
            self._pending_heartbeat_state = state
            if sent_progress is not None:
                self._pending_heartbeat_progress[slot_id] = sent_progress
            if self._coalesced_heartbeat_task is None:
                self._coalesced_heartbeat_task = asyncio.create_task(
                    self._flush_coalesced_heartbeat(),
                    name="validator-heartbeat-coalescer",
                )
            task = self._coalesced_heartbeat_task
        # Bounded callers may time out without cancelling the shared flush for
        # another slot. The next snapshot contains every slot's latest state.
        return await asyncio.shield(task)

    async def _flush_coalesced_heartbeat(self) -> bool:
        """Wait for the next wall second, then publish the newest snapshot."""
        try:
            while True:
                delay = (
                    self._last_heartbeat_timestamp + 1 - self._heartbeat_clock.time()
                )
                if delay > 0:
                    await self._heartbeat_clock.sleep(delay)
                async with self._active_heartbeat_lock:
                    if (
                        int(self._heartbeat_clock.time())
                        <= self._last_heartbeat_timestamp
                    ):
                        continue
                    state = self._pending_heartbeat_state or "idle"
                    self._pending_heartbeat_state = None
                    sent_progress = self._pending_heartbeat_progress
                    self._pending_heartbeat_progress = {}
                    try:
                        delivered = await self._report_heartbeat_unlocked(state)
                        if delivered:
                            for slot_id, progress in sent_progress.items():
                                self._record_delivered_progress(slot_id, progress)
                        return delivered
                    finally:
                        self._coalesced_heartbeat_task = None
        except BaseException:
            async with self._active_heartbeat_lock:
                if self._coalesced_heartbeat_task is asyncio.current_task():
                    self._coalesced_heartbeat_task = None
            raise

    def _record_delivered_progress(
        self, slot_id: str, progress: BenchmarkProgress
    ) -> None:
        slot = self._slots[slot_id]
        slot.last_progress_heartbeat_monotonic = time.monotonic()
        slot.last_progress_bucket = self._progress_bucket(progress)

    async def _report_heartbeat_unlocked(
        self,
        state: ValidatorRuntimeState,
        *,
        active_snapshot: tuple[UUID | None, BenchmarkProgress | None] | None = None,
    ) -> bool:
        """Best-effort signed build + runtime report; never gate validator work."""
        del active_snapshot  # v10+ always signs one atomic all-slot snapshot.
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
            timestamp = int(self._heartbeat_clock.time())
            if timestamp <= self._last_heartbeat_timestamp:
                raise RuntimeError("heartbeat wall-clock rate limit was bypassed")
            self._last_heartbeat_timestamp = timestamp
            system_metrics = self._collect_system_metrics()
            # Every heartbeat re-decides, not just the sweep boundary: a disk
            # that fills during a 90-minute run must show up as constrained on
            # the next heartbeat, not one whole sweep later. Recovery narrows
            # nothing here (``_healthy_slots &=`` below only ever shrinks); the
            # next sweep's preflight is what re-opens the slots.
            self._refresh_resource_admission(system_metrics)
            capabilities, stack = validator_capabilities_and_stack()
            capability_probe = getattr(
                self._dittobench, "scorer_benchmark_capability", None
            )
            # No probe ran (no dittobench client is wired up). The heartbeat
            # says exactly that rather than omitting the field, so "this
            # validator observed nothing" stays distinguishable from "this
            # validator is too old to observe anything".
            scorer_benchmarks = ScorerBenchmarkCapability(
                status="legacy_v2",
                supported_bench_versions=(),
                probe=ScorerLivenessProbe(outcome="not_probed", observed_at=timestamp),
            )
            if capability_probe is not None:
                observed = capability_probe(stack)
                if inspect.isawaitable(observed):
                    scorer_benchmarks = await observed
            # The freshly probed scorer capacity is a ceiling, not a kill
            # switch: narrow the advertisement to what the scorer can serve so a
            # worker briefly ahead of its scorer keeps offering the slots that
            # do work. See :meth:`_available_slots`.
            self._healthy_slots &= self._available_slots()
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
            self._apply_lease_roster(response, advertised=capacity)
            return response.accepted
        except Exception as e:  # noqa: BLE001 - observability must never gate work
            # Reached when the heartbeat never landed: unreachable platform,
            # rejection, malformed body. Nothing is cancelled from here, and
            # that is structural rather than a rule to remember -- the roster is
            # only ever read from a response that exists.
            self._platform_accepted = False
            logger.warning("validator heartbeat failed (scoring continues): %s", e)
            return False

    def _apply_lease_roster(
        self, response: ValidatorHeartbeatResponse, *, advertised: BenchmarkCapacity
    ) -> None:
        """Stop any advertised run whose lease the platform no longer lists.

        ``advertised`` is the capacity this very heartbeat carried, and pairing
        it with that heartbeat's own answer is what makes the diff sound without
        comparing clocks: the platform read its ledger while handling the
        request, hence strictly after this worker had claimed every slot named in
        it. See :mod:`ditto.validator.lease_roster`.

        This is the validator voluntarily standing down because the platform
        told it the lease is gone. It is not the platform inferring idleness from
        silence, which is the thing ditto-platform#496 exists to forbid: a slot
        that has never reported is *more* protected here, not less, because a
        lease the platform still holds is listed whether or not it has heard
        progress on it.
        """
        roster = read_roster(response)
        if isinstance(roster, RosterUnknown):
            logger.debug("heartbeat carried no lease roster: %s", roster.reason)
            return
        for slot_id, agent_id in plan_cancellations(roster, advertised=advertised):
            slot = self._slots.get(slot_id)
            if slot is None or slot.active_agent_id != agent_id:
                # The run finished, or the slot moved on, between building the
                # request and reading its answer. A normal no-op: any score it
                # already produced is refused with a clean 409 if the lease
                # really did go away.
                continue
            if slot.revoked.is_set():
                continue
            logger.warning(
                "platform no longer holds the lease for agent %s on %s; "
                "cancelling the run rather than spending the rest of the lease "
                "on a score it will refuse",
                agent_id,
                slot_id,
            )
            slot.revoked.set()

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
        sent_progress = self._benchmark_progress
        active_snapshot = (self._active_agent_id, sent_progress)
        task = asyncio.create_task(
            asyncio.wait_for(
                self._report_heartbeat(
                    "running_benchmark", active_snapshot=active_snapshot
                ),
                timeout=_ACTIVE_TELEMETRY_HARD_TIMEOUT_SECONDS,
            ),
            name="validator-active-heartbeat",
        )
        try:
            delivered = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_ACTIVE_TELEMETRY_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # ``wait_for`` used to cancel the send here. During a parallel claim
            # burst the heartbeat lock serializes siblings, so a healthy send
            # could cross the two-second caller budget before it even reached
            # the network. Its first ``preparing`` snapshot then vanished until
            # the scorer's later progress loop started, making occupied slots
            # read as "Benchmark progress not reported". Detach the bounded task
            # instead: scoring continues now and the all-slot snapshot still has
            # a chance to land.
            self._background_heartbeat_tasks.add(task)
            task.add_done_callback(self._finish_background_heartbeat)
            logger.warning(
                "validator progress heartbeat exceeded caller budget; "
                "delivery continues in background"
            )
            delivered = False
        except asyncio.CancelledError:
            # Worker shutdown is different from the caller's telemetry budget:
            # do not leave a detached send behind when the owning task is gone.
            task.cancel()
            raise
        return delivered

    def _finish_background_heartbeat(self, task: asyncio.Task[bool]) -> None:
        """Consume one detached heartbeat result and release its strong ref."""
        self._background_heartbeat_tasks.discard(task)
        if task.cancelled():
            return
        try:
            delivered = task.result()
        except TimeoutError:
            logger.warning(
                "validator progress heartbeat reached the background hard "
                "timeout; scoring continues"
            )
        except Exception as error:  # noqa: BLE001 - telemetry remains fail-open
            logger.warning(
                "validator background progress heartbeat failed; scoring continues: %s",
                error,
            )
        else:
            if not delivered:
                logger.warning(
                    "validator background progress heartbeat was not accepted; "
                    "scoring continues"
                )

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
        """Reset progress throttling and publish artifact preparation promptly.

        Idempotent per lease. ``_score_job`` claims the slot before handing off
        to inference activation so the slot is never silently occupied, and the
        scoring path then announces the run proper; the second call must not
        re-emit ``preparing`` or discard the progress already published for this
        exact lease.
        """
        if (
            self._active_agent_id == agent_id
            and self._active_ticket_deadline == ticket_deadline
            and self._benchmark_progress is not None
        ):
            return
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
            rank_shares=self._config.koth_rank_shares,
            dethrone_z=self._config.koth_dethrone_z,
        )
        weights = apply_miner_emission_cap(
            miner_weights,
            miner_share=self._config.miner_emission_share,
            burn_hotkey=self._config.burn_hotkey,
        )
        champion = select_champion(
            registered_entries,
            margin=self._config.koth_margin,
            dethrone_z=self._config.koth_dethrone_z,
        )
        king_fingerprint = self._king_fingerprint(champion)
        if not miner_weights:
            logger.info(
                "ledger has no positive scores; routing 100% of miner emission to burn"
            )
        if not await self._validator_permitted() or not await self._stake_sufficient():
            # No permit / demonstrably short stake → the chain would reject the
            # submission anyway; skip it (loudly) rather than burn an epoch on a
            # guaranteed rejection.
            return _WeightOutcome(
                leaderboard=leaderboard,
                weights=weights,
                king_fingerprint=king_fingerprint,
            )
        await self._log_commit_reveal_mode()
        submitted = await self._put_weights_with_retry(weights)
        return _WeightOutcome(
            leaderboard=leaderboard,
            weights=weights,
            submitted=submitted,
            king_fingerprint=king_fingerprint,
        )

    @staticmethod
    def _king_fingerprint(
        champion: LedgerEntry | None,
    ) -> tuple[str, UUID, float, int | None] | None:
        if champion is None:
            return None
        return (
            champion.miner_hotkey,
            champion.agent_id,
            champion.composite,
            champion.bench_version,
        )

    async def _observe_platform_king(
        self,
    ) -> tuple[bool, tuple[str, UUID, float, int | None] | None]:
        """Return ``(available, fingerprint)`` from the verified public ledger."""
        try:
            ledger = await self._platform.get_ledger()
        except PlatformError as e:
            logger.warning("event-driven king check failed: %s", e)
            return False, None
        champion = select_champion(
            ledger.entries,
            margin=self._config.koth_margin,
            dethrone_z=self._config.koth_dethrone_z,
        )
        return True, self._king_fingerprint(champion)

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

    async def _run_top5_confirmation_lane(
        self,
        *,
        stop_requested: asyncio.Event | None = None,
        drain_requested: asyncio.Event | None = None,
        _member_agent_id: UUID | None = None,
        _plan: Top5ConfirmationPlan | None = None,
    ) -> None:
        """Claim and execute bounded append-only work for the current top five.

        The outer invocation fans the cohort out concurrently. Each child asks
        the platform for one member and binds execution to the distinct slot the
        platform leases back. This lets an idle multi-slot host help several
        members at once, while the platform remains authoritative for the
        operator cap, seed uniqueness, and one live lease per slot.
        """
        plan = _plan
        if plan is None:
            try:
                ledger = await self._platform.get_ledger()
            except PlatformError as exc:
                logger.warning("top-five confirmation ledger fetch failed: %s", exc)
                return
            plan = top5_confirmation_set(
                ledger.entries,
                current_version=self._current_bench_version,
                margin=self._config.koth_margin,
                dethrone_z=self._config.koth_dethrone_z,
                tail_size=self._config.koth_tail_size,
                baseline_seeds=self._config.koth_confirmation_seeds,
                max_seeds=self._config.top5_max_confirmation_seeds,
                catch_up_rate=self._config.top5_catch_up_rate,
                # Operator policy, re-read on every ledger poll so a Backroom
                # change reaches the fleet without a validator restart. A stale
                # last-known-good ledger omits it and falls back to emissions.
                cohort_size=ledger.continual_retest_cohort_size,
                max_cohort_size=self._config.top5_max_cohort_size,
            )
        if plan is None:
            return
        if _member_agent_id is None:
            # Ordinary queue work has already polled empty before this lane is
            # entered. Use every locally healthy slot for catch-up rather than
            # serializing the cohort through slot-0. The platform may grant
            # fewer leases than we ask for (operator cap, global fairness, or a
            # sibling validator already covering the seed); those 409s are
            # harmless and the next sweep reconciles from durable evidence.
            #
            # One member appears once in this fan-out, so a single validator
            # never runs two seeds for the same agent concurrently. Different
            # validators can -- and do -- take distinct missing seeds for that
            # agent, giving a five-validator fleet five-way per-agent catch-up
            # while up to five cohort members run on each host.
            await asyncio.gather(
                *(
                    self._run_top5_confirmation_lane(
                        stop_requested=stop_requested,
                        drain_requested=drain_requested,
                        _member_agent_id=member.entry.agent_id,
                        _plan=plan,
                    )
                    for member in plan.members
                )
            )
            return
        for member in plan.members:
            if member.entry.agent_id != _member_agent_id:
                continue
            if self._new_work_blocked(stop_requested, drain_requested):
                return
            entry = member.entry
            job = None
            slot_token = None
            ticket_claimed = False
            try:
                job = await self._platform.request_top5_confirmation_job(
                    champion_agent_id=plan.champion.agent_id,
                    member_agent_id=entry.agent_id,
                )
                # Mirrors the canonical path's slot-mismatch guard: binding an
                # unserved slot below would raise KeyError out of the lane and
                # take the rest of the sweep's confirmations with it.
                if job.slot_id not in self._slots:
                    logger.warning(
                        "top-five confirmation leased unserved slot %s for "
                        "agent %s; this validator serves %s",
                        job.slot_id,
                        entry.agent_id,
                        sorted(self._slots),
                    )
                    await self._report_ticket_failed(
                        job, "infrastructure", "confirmation_slot_not_served"
                    )
                    continue
                # This lane runs in the sweep body, after the per-slot gather
                # and therefore outside the context ``run_slot`` establishes.
                # Every per-slot write below -- the active ticket, published
                # progress, and the slot key ``_report_heartbeat`` files pending
                # progress under -- resolves through ``_CURRENT_SLOT``, so
                # without this bind the whole retest reported against the
                # default ``slot-0`` while the platform leased ``job.slot_id``.
                # The platform drops slot progress it cannot match to a live
                # ticket on that exact slot, so the assigned slot published
                # nothing at all and read as frozen for its whole lease.
                # Bind per lease rather than around the lane: the slot belongs
                # to the ticket, not to the lane. Plain awaits inherit this
                # context, and the scorer's progress callback runs under tasks
                # created after this point, which copy it at creation.
                slot_token = _CURRENT_SLOT.set(job.slot_id)
                expected_seeds = tuple(member.seeds_to_score)
                received_seeds = tuple(
                    dataset.seed for dataset in job.confirmation_datasets
                )
                if (
                    job.bench_version is not None
                    and job.bench_version >= 3
                    and not received_seeds
                ):
                    logger.warning(
                        "top-five confirmation dataset contract missing pins "
                        "agent=%s local_plan=%s",
                        entry.agent_id,
                        expected_seeds,
                    )
                    await self._report_ticket_failed(
                        job, "infrastructure", "confirmation_dataset_pins_missing"
                    )
                    continue
                if len(received_seeds) != len(set(received_seeds)):
                    logger.warning(
                        "top-five confirmation dataset contract contains duplicate "
                        "pins agent=%s received=%s",
                        entry.agent_id,
                        received_seeds,
                    )
                    await self._report_ticket_failed(
                        job, "infrastructure", "confirmation_dataset_pins_duplicated"
                    )
                    continue
                datasets = (
                    job.confirmation_datasets
                    if job.confirmation_datasets
                    else [
                        ConfirmationDatasetPin(
                            seed=seed,
                            dataset_sha256="0" * 64,
                            run_size="full",
                        )
                        for seed in expected_seeds
                    ]
                )
                # Set before the claim, not after: ``_begin_active_ticket``
                # occupies the slot as its first act, so a failure part-way
                # through must still leave the slot clearable below.
                ticket_claimed = True
                await self._begin_active_ticket(
                    job.agent_id,
                    job.deadline,
                    job.bench_version or DEFAULT_BENCH_VERSION,
                )
                broker = await self._activate_ticket_inference(job)
                try:
                    report = await self._evaluate_confirmation_report(
                        entry.agent_id,
                        entry.sha256,
                        datasets=datasets,
                        bench_version=job.bench_version,
                        inference_session_id=(
                            broker.session_id if broker is not None else None
                        ),
                        inference_grant_id=(
                            job.inference.grant_id
                            if broker is not None and job.inference is not None
                            else None
                        ),
                        inference_slot_id=(job.slot_id if broker is not None else None),
                        inference_ticket_deadline=(
                            job.deadline if broker is not None else None
                        ),
                        ticket_deadline=job.deadline,
                    )
                finally:
                    if broker is not None:
                        await self._dittobench.cancel_inference_session(
                            broker.session_id
                        )
                if report is None:
                    await self._report_ticket_failed(
                        job, "scoring_error", "confirmation_run_produced_no_report"
                    )
                    continue
                await self._platform.submit_top5_confirmation_score(
                    entry.agent_id,
                    report=report,
                    ticket_deadline=job.deadline,
                )
            except LeaseDeadlineError as exc:
                logger.warning(
                    "top-five confirmation reached the lease deadline "
                    "champion=%s member=%s: %s",
                    plan.champion.agent_id,
                    entry.agent_id,
                    exc,
                )
                if job is not None:
                    # Same attribution rule as the canonical lane: running out
                    # of lease is not this host's infrastructure failing.
                    await self._report_ticket_failed(
                        job, "scoring_error", failure_detail(exc)
                    )
            except (PlatformError, DittobenchError) as exc:
                logger.warning(
                    "top-five confirmation failed champion=%s member=%s: %s",
                    plan.champion.agent_id,
                    entry.agent_id,
                    exc,
                )
                if job is not None:
                    await self._report_ticket_failed(
                        job, "infrastructure", failure_detail(exc)
                    )
            finally:
                # Release whatever this iteration actually claimed. Matching on
                # ``entry.agent_id`` instead would leak the slot for the rest of
                # the lease if a lease ever came back for a different agent than
                # the member requested: the claim is made from ``job.agent_id``,
                # so a divergence left the slot occupied with no way to revoke
                # it. Both the clear and its heartbeat must run before the slot
                # context is unwound, or they land on the wrong slot.
                if ticket_claimed:
                    self._clear_active_ticket()
                    await self._report_heartbeat("polling")
                if slot_token is not None:
                    _CURRENT_SLOT.reset(slot_token)

    async def _evaluate_confirmation_report(
        self,
        agent_id: UUID,
        expected_sha256: str,
        *,
        datasets: Sequence[ConfirmationDatasetPin],
        bench_version: int | None,
        inference_session_id: str | None = None,
        inference_grant_id: UUID | None = None,
        inference_slot_id: str | None = None,
        inference_ticket_deadline: datetime | None = None,
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport | None:
        """Evaluate fresh seeds and package one signed append-only receipt.

        Every seed is bounded by the same lease, so a seed that hangs cannot
        consume the budget the remaining seeds -- and the hand-back -- need.
        """
        reports: list[ScoreReport] = []
        for dataset in datasets:
            if (
                ticket_deadline is not None
                and lease_budget_seconds(ticket_deadline) <= 0
            ):
                logger.warning(
                    "top-five confirmation ran out of lease for agent %s before "
                    "seed %s; resolving the ticket with what has been scored",
                    agent_id,
                    dataset.seed,
                )
                break
            try:
                reports.append(
                    await self._evaluate(
                        agent_id,
                        expected_sha256,
                        seed=dataset.seed,
                        dataset_sha256=(
                            dataset.dataset_sha256
                            if bench_version is not None and bench_version >= 3
                            else None
                        ),
                        run_size=(
                            dataset.run_size
                            if bench_version is not None and bench_version >= 3
                            else None
                        ),
                        bench_version=bench_version,
                        progress_callback=self._on_dittobench_progress,
                        inference_session_id=inference_session_id,
                        inference_grant_id=inference_grant_id,
                        inference_slot_id=inference_slot_id,
                        inference_ticket_deadline=inference_ticket_deadline,
                        ticket_deadline=ticket_deadline,
                    )
                )
            except (PlatformError, DittobenchError) as exc:
                logger.warning(
                    "top-five confirmation seed failed agent=%s seed=%s: %s",
                    agent_id,
                    dataset.seed,
                    exc,
                )
        if not reports:
            return None
        ordered = sorted(reports, key=lambda report: (report.composite, report.seed))
        representative = ordered[len(ordered) // 2]
        pairs = sorted((report.seed, report.composite) for report in reports)
        return representative.model_copy(
            update={
                "confirmation_seeds": [seed for seed, _ in pairs],
                "confirmation_composites": [value for _, value in pairs],
                "composite_stderr": _pooled_confirmation_stderr(
                    [value for _, value in pairs], representative.composite_stderr
                ),
            }
        )

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
        self,
        job: JobResponse,
        reason: FailJobReason,
        detail: str | None = None,
    ) -> None:
        """Best-effort hand-back of a failed ticket for immediate reissue.

        ``detail`` is the reporter's own code behind ``reason``, from
        :func:`~ditto.validator.errors.failure_detail`. ``reason`` is a
        three-value class chosen to drive the platform's reissue policy, so it
        says how the platform should respond and nothing about what happened;
        ditto-subnet#279 classified twelve dead ``mnemo*`` leases off it and
        still could not name the fault. Optional on both sides of the wire, so
        omitting it is always safe.

        Closing the live lease lets the next :meth:`request_job` mint a fresh
        ticket instead of resuming the failed attempt. Strictly best-effort: an
        old platform without ``/validator/job/fail``, or any transport/validation
        error, must never crash the sweep — the ticket then simply expires on its
        own deadline exactly as it did before this endpoint existed.

        Bounded, because "best-effort" must not mean "unbounded". This is the
        call that turns a lease into a resolved ticket, and it is reached from
        the abort path with only the reporting margin left; a platform that
        accepts the connection and then stalls would otherwise spend the margin
        here and produce the silent expiry the abort exists to prevent.
        """
        try:
            await asyncio.wait_for(
                self._platform.report_ticket_failed(job, reason, detail),
                timeout=_FAIL_REPORT_TIMEOUT_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 - hand-back is best-effort telemetry
            logger.warning(
                "handing back failed ticket for agent %s did not land "
                "(ticket will expire on its own): %s",
                job.agent_id,
                e,
            )

    async def _activate_ticket_inference(
        self, job: JobResponse
    ) -> InferenceBrokerSession | None:
        """Exchange and bind inference for one v7 ticket.

        Legacy tickets must drain before fleet-wide enforcement. They never
        switch to the mutable platform route because doing so would change the
        frozen v6 provider and no-think semantics mid-lease.
        """
        bench_version = job.bench_version or DEFAULT_BENCH_VERSION
        if bench_version < 7:
            if getattr(self._config, "inference_proxy_required", False) is True:
                raise ValidatorInfrastructureError(
                    "legacy benchmark ticket remained after platform inference "
                    "enforcement; drain or expire it before cutover"
                )
            return None
        if job.inference is None:
            raise ValidatorInfrastructureError(
                f"benchmark v{bench_version} requires platform inference "
                "but the ticket carried no capability"
            )

        broker = await self._dittobench.prepare_inference_session()
        try:
            exchange = await self._platform.exchange_inference_grant(
                job.inference.grant_id,
                broker.broker_public_key,
                job.inference.exchange_url,
            )
            route_invalid = (
                exchange.provider is None
                or exchange.profile_revision is None
                or exchange.model is None
                or job.inference.provider is None
                or job.inference.profile_revision is None
                or exchange.provider != job.inference.provider
                or exchange.profile_revision != job.inference.profile_revision
                or exchange.model not in job.inference.allowed_models
            )
            if (
                exchange.grant_id != job.inference.grant_id
                or exchange.proxy_url != job.inference.proxy_url
                or exchange.expires_at > job.inference.expires_at
                or exchange.expires_at > job.deadline
                or route_invalid
            ):
                raise PlatformError("inference exchange escaped ticket bounds")
            await self._dittobench.activate_inference_session(
                broker,
                grant_id=exchange.grant_id,
                agent_id=job.agent_id,
                slot_id=job.slot_id,
                ticket_deadline=job.deadline,
                bearer=exchange.bearer,
                proxy_url=exchange.proxy_url,
                generation=exchange.generation,
                expires_at=exchange.expires_at,
                provider=exchange.provider,
                profile_revision=exchange.profile_revision,
                model=exchange.model,
            )
        except BaseException:
            await self._dittobench.cancel_inference_session(broker.session_id)
            raise
        return broker

    async def _score_job_within_lease(self, job: JobResponse) -> ScoreReport:
        """Score one ticket under a hard bound derived from its own lease.

        The invariant this enforces: **a validator holding a ticket always
        resolves it before the lease expires.** Silence is not a neutral
        outcome — an unresolved ticket reads in the ledger exactly like a
        validator that died, and it holds one of the fleet's few scoring slots
        for the full lease while saying nothing.

        The poll loop is bounded by the same budget (see
        :func:`ditto.validator.config.run_budget_seconds`), so in the ordinary
        case this never fires. It exists because the poll is not the only place a
        ticket can hang: the artifact fetch, the inference grant exchange (this
        module already documents it as "unbounded work ... can hold a slot for
        many minutes"), the submit, and the post-run cancel are all awaits with
        no bound of their own beyond a per-request HTTP timeout that a
        responsive-but-stuck peer never trips. One outer bound covers all of
        them, which is also what keeps a single hanging agent from holding a
        slot for the whole lease while the rest of the queue waits.

        The margin is left deliberately outside the bound so the caller still
        has time to land the failure report.
        """
        slot = self._slot_state()
        # A revocation belongs to the lease that provoked it, never to the next
        # one this slot picks up. If the new lease is also gone the very next
        # heartbeat says so again.
        slot.revoked.clear()
        budget = lease_budget_seconds(job.deadline)
        if budget <= 0:
            raise LeaseDeadlineError(
                f"ticket for agent {job.agent_id} has less than the reporting "
                f"margin left before {job.deadline.isoformat()}; not starting"
            )
        try:
            async with asyncio.timeout(budget):
                return await self._score_until_revoked(job, slot)
        except TimeoutError as error:
            raise LeaseDeadlineError(
                f"scoring agent {job.agent_id} did not resolve within the "
                f"{budget:.0f}s its lease could fund before "
                f"{job.deadline.isoformat()}"
            ) from error

    async def _score_until_revoked(
        self, job: JobResponse, slot: _SlotState
    ) -> ScoreReport:
        """Score, but stop the moment the platform says the lease is gone.

        Cancellation works exactly the way ``asyncio.timeout`` already makes it
        work here: a watchdog cancels *this* task, so the ``CancelledError``
        lands on whichever await the run is actually parked on. That is what
        lets ``DittobenchClient._poll``'s own ``except CancelledError`` reach the
        scorer and kill the run's container, and it is why the scoring call stays
        inline -- moving it into a child task would put it outside the reach of
        the enclosing lease-deadline timeout and quietly break ditto-subnet#279.

        The ``CancelledError`` is only re-labelled when the revocation is what
        caused it. If the lease deadline fired too, #279 keeps the attribution:
        that path must still hand the ticket back as ``scoring_error``, and a
        revocation racing in at the very end must not turn it into silence.
        """
        running = asyncio.current_task()

        async def watch_for_revocation() -> None:
            await slot.revoked.wait()
            if running is not None:
                running.cancel()

        watchdog = asyncio.create_task(watch_for_revocation())
        try:
            return await self._score_job(job)
        except asyncio.CancelledError:
            if not slot.revoked.is_set() or lease_budget_seconds(job.deadline) <= 0:
                raise
            # Balance the watchdog's cancel so an enclosing ``asyncio.timeout``
            # does not later read this as its own expiry.
            if running is not None:
                running.uncancel()
            raise LeaseRevokedError(
                f"platform no longer holds the lease for agent {job.agent_id} "
                f"on {job.slot_id}; stopping the run"
            ) from None
        finally:
            # Cancelled, never awaited. The watchdog has no await between
            # observing the event and calling ``cancel``, so cancelling it here
            # provably stops it from firing late into the next lease -- while
            # awaiting it would risk swallowing this task's *own* cancellation
            # and losing the lease-deadline abort it was meant to preserve.
            watchdog.cancel()

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
            and not seed_matches(
                job.dataset_seed_block_hash,
                job.agent_id,
                job.seed,
                validator_hotkey=(
                    self._config.validator_hotkey
                    if job.seed_scope == "validator"
                    else None
                ),
            )
        ):
            raise PlatformError(
                f"ticket seed {job.seed} for agent {job.agent_id} does not "
                f"re-derive from pinned block hash "
                f"{job.dataset_seed_block_hash!r}; refusing to score"
            )
        # Claim the slot publicly BEFORE activating inference. Exchanging the
        # grant and standing up the broker session is unbounded work -- with
        # several slots contending for inference it can hold a slot for many
        # minutes -- and until this ran the slot published nothing at all. The
        # platform then had a live lease with no progress against it, which
        # renders as "Benchmark progress not reported" and, worse, reads to the
        # lease-liveness gate as a slot sitting idle. ``_evaluate_and_submit``
        # re-announces ``preparing`` for the run proper; re-announcing the first
        # stage of a lease is explicitly allowed and simply rebaselines.
        await self._begin_active_ticket(
            job.agent_id, job.deadline, job.bench_version or DEFAULT_BENCH_VERSION
        )
        try:
            broker = await self._activate_ticket_inference(job)
        except Exception:
            # Nothing downstream will clear the slot we just claimed.
            self._clear_active_ticket()
            raise
        inference_session_id = broker.session_id if broker is not None else None
        inference_grant_id = (
            job.inference.grant_id
            if broker is not None and job.inference is not None
            else None
        )
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
                inference_grant_id=inference_grant_id,
                inference_slot_id=(
                    job.slot_id if inference_session_id is not None else None
                ),
            )
        finally:
            if broker is not None:
                await self._dittobench.cancel_inference_session(broker.session_id)

    async def _evaluate(
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
        inference_grant_id: UUID | None = None,
        inference_slot_id: str | None = None,
        inference_ticket_deadline: datetime | None = None,
        ticket_deadline: datetime | None = None,
    ) -> ScoreReport:
        """Run one re-score while managing its benchmark heartbeat.

        ``seed`` pins the dataset seed. ``dataset_sha256`` (from the ticket)
        selects the canonical /v1/score path, where the engine regenerates that
        exact dataset and fails on a hash mismatch (tamper-evidence). Historical
        unticketed sweeps may pass a common ``seed`` (CRN) without a dataset hash.
        """
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
                progress_callback=progress_callback,
                inference_session_id=inference_session_id,
                inference_grant_id=inference_grant_id,
                inference_slot_id=inference_slot_id,
                inference_ticket_deadline=inference_ticket_deadline,
                ticket_deadline=ticket_deadline,
            )
        finally:
            heartbeat_stop.set()
            await heartbeat_task
            await self._report_heartbeat(
                "running_benchmark" if self._active_agent_id == agent_id else "polling"
            )

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
        inference_grant_id: UUID | None = None,
        inference_slot_id: str | None = None,
        inference_ticket_deadline: datetime | None = None,
        ticket_deadline: datetime | None = None,
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
            inference_grant_id=inference_grant_id,
            inference_agent_id=agent_id if inference_session_id is not None else None,
            inference_slot_id=inference_slot_id,
            inference_ticket_deadline=inference_ticket_deadline,
            ticket_deadline=ticket_deadline,
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
        inference_grant_id: UUID | None = None,
        inference_slot_id: str | None = None,
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
                inference_grant_id=inference_grant_id,
                inference_slot_id=inference_slot_id,
                inference_ticket_deadline=(
                    ticket_deadline if inference_session_id is not None else None
                ),
                ticket_deadline=ticket_deadline,
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
        last_submit_at: float | None = None
        while not stop.is_set():
            if drain_requested is not None and drain_requested.is_set():
                # The scoring loop is the sole drain-acknowledgement owner: it
                # verifies that this task is inactive before publishing
                # ``drained``. The weight loop only remains quiescent here.
                while drain_requested.is_set() and not stop.is_set():
                    await self._sleep_or_stop(stop, 0.05)
                if not stop.is_set() and last_submit_at is not None:
                    # A drain interrupts the cadence sleep. Resume on the
                    # REMAINDER of the interrupted epoch, not a fresh full one:
                    # a deploy that drains late in an epoch used to pay a second
                    # full epoch before weights resumed, up to 72 minutes of
                    # avoidable propagation delay per restart on SN118.
                    #
                    # The remainder is measured from this worker's own last
                    # submission rather than from the chain window, because
                    # ``_seconds_until_weight_window`` fails open to 0 when the
                    # ``LastUpdate`` read is unavailable. Resuming on that alone
                    # would resubmit immediately during a Pylon read outage --
                    # exactly the rate-limit race the original full-epoch sleep
                    # existed to prevent. Local elapsed time is always known.
                    epoch_seconds = max(float(self._config.epoch_seconds), chain_floor)
                    remaining = epoch_seconds - (time.monotonic() - last_submit_at)
                    if remaining > 0:
                        await self._sleep_or_stop_or_drain(
                            stop, remaining, drain_requested
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
                    "weight request accepted by Pylon: accepted=%s miner(s)=%d; "
                    "on-chain state is observed separately",
                    outcome.submitted,
                    len(outcome.weights),
                )
            except Exception:  # noqa: BLE001 - weights retry next epoch
                logger.exception("weight epoch failed; retrying next epoch")
            finally:
                self._weights_active = False
                last_submit_at = time.monotonic()
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
            if outcome.submitted:
                await self._wait_for_king_or_weight_window(
                    stop,
                    epoch_seconds=epoch_seconds,
                    baseline=outcome.king_fingerprint,
                    drain_requested=drain_requested,
                )
            else:
                # A rejected platform/chain attempt must not spin, but it also
                # must not suppress a newly signed king for an entire epoch.
                await self._sleep_or_stop_or_drain(
                    stop, self._config.sweep_seconds, drain_requested
                )

    async def _wait_for_king_or_weight_window(
        self,
        stop: asyncio.Event,
        *,
        epoch_seconds: float,
        baseline: tuple[str, UUID, float, int | None] | None,
        drain_requested: asyncio.Event | None,
    ) -> None:
        """Watch signed ledger receipts while respecting commit-reveal cadence.

        Local scores wake this loop through ``_ledger_changed``; scores from
        other validators are observed on the normal sweep poll. A changed king
        is remembered immediately, but submission remains gated by the chain's
        LastUpdate/tempo window. This gives the new king the earliest legal
        commit without generating ``SettingWeightsTooFast`` churn.
        """
        observed = baseline
        # A successful local submission is authoritative even if the RPC has
        # not indexed LastUpdate yet. Never let a temporarily stale chain read
        # collapse this guard to zero and create SettingWeightsTooFast churn.
        local_not_before = time.monotonic() + epoch_seconds
        while not stop.is_set():
            if drain_requested is not None and drain_requested.is_set():
                return
            chain_delay = await self._seconds_until_weight_window(epoch_seconds)
            delay = max(chain_delay, local_not_before - time.monotonic())
            if delay <= 0:
                return
            wait_seconds = min(
                delay,
                max(1.0, float(self._config.sweep_seconds)),
            )
            if self._ledger_changed.is_set():
                self._ledger_changed.clear()
            else:
                await self._sleep_or_stop_or_drain(stop, wait_seconds, drain_requested)
                if stop.is_set() or (
                    drain_requested is not None and drain_requested.is_set()
                ):
                    return
            available, current = await self._observe_platform_king()
            if available and current != observed:
                logger.info(
                    "signed king changed from %s to %s; scheduling weights for "
                    "the earliest legal commit-reveal window",
                    observed,
                    current,
                )
                observed = current

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

    @staticmethod
    async def _sleep_or_interrupt(
        seconds: float, *events: asyncio.Event | None
    ) -> None:
        """Sleep up to ``seconds``, returning early if any event is set.

        The in-sweep twin of :meth:`_sleep_or_stop_or_drain`, which requires a
        non-optional ``stop``. Inside ``run_once`` both the stop and drain events
        are optional, so a waiting slot has to tolerate having neither and still
        honour whichever it was given -- otherwise an idle slot's wait would add
        its full duration to every shutdown and drain.
        """
        waits = [
            asyncio.create_task(event.wait()) for event in events if event is not None
        ]
        if not waits:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait(
                waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

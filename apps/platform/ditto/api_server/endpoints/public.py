"""Public, unauthenticated read endpoints for the subnet dashboard.

Three surfaces, all open (no credentials) and fronting the same DB the
validator-gated ``/scoring/scores`` reads:

* **Aggregate leaderboard / health** (``/leaderboard``, ``/health``): best score
  per payment-time coldkey, composite plus tool/memory means and rank, never
  exposing per-case answer-key detail. This half stays aggregate-only.
* **Submission lifecycle** (``/activity``, ``/agent/{id}/pipeline``): recent
  uploads, public pipeline stage, safe screening evidence, and accepted numeric
  scores as they arrive. In-progress score rows carry reproducibility inputs but
  omit validator identity, signatures, ticket leases, and scorer internals.
* **Per-submission transparency** (``/submissions``, ``/agent/{id}/scores``): the
  k=3 record for a finalized agent: *which* validators scored it, each one's
  exact numbers + signature, the median the platform finalized on, and the pinned
  dataset (seed + sha256). This deliberately exposes ``validator_hotkey`` (a
  public on-chain identity) and the raw ``seed`` so anyone can reproduce and audit
  a score; because the platform draws the seed after screening, publishing it
  post-hoc never lets a miner pre-overfit. It still omits the per-case answer key.
  See ``docs/public-telemetry.md``.

Responses are cacheable (``max-age=30``) so a CDN / the dashboard can front this
cheaply; the underlying rows only change when a sweep records a new score. The
leaderboard includes a read-only projection of the validator's frozen KOTH fold
so raw score rank is never mistaken for the emissions champion. Validators remain
the authority that independently computes and submits the real weight vector.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import (
    BenchDatasetConfig,
    BenchGradingConfig,
    BenchHarnessConfig,
    CreateScreeningDisputeRequest,
    CreateScreeningDisputeResponse,
    PublicActivityEntry,
    PublicActivityResponse,
    PublicAgentSummary,
    PublicArtifactDownload,
    PublicArtifactRelease,
    PublicAuditEntry,
    PublicAuditResponse,
    PublicBenchConfigResponse,
    PublicBenchCorpusEntry,
    PublicBenchCorpusResponse,
    PublicBenchGlossaryResponse,
    PublicBenchIntegrity,
    PublicBenchmarkProgress,
    PublicBenchmarkQualityFactor,
    PublicBenchmarkRelease,
    PublicBenchmarkTimelinePoint,
    PublicBenchmarkTimelineResponse,
    PublicBenchRolloutResponse,
    PublicBenchVersionDoc,
    PublicCaseResult,
    PublicCategoryDoc,
    PublicCategoryStat,
    PublicChainWeight,
    PublicChainWeightsResponse,
    PublicCompositeBreakdown,
    PublicConfirmationProgress,
    PublicConfirmationScore,
    PublicConfirmationSubject,
    PublicDatasetReveal,
    PublicDethroneDecision,
    PublicEfficiencyCohortMember,
    PublicEfficiencySnapshotResponse,
    PublicEfficiencyStatus,
    PublicEmissionRecipient,
    PublicHealthResponse,
    PublicInferenceRun,
    PublicKothEmissions,
    PublicLeaderboardEntry,
    PublicLeaderboardFamily,
    PublicLeaderboardFamilyMember,
    PublicLeaderboardResponse,
    PublicMetricDoc,
    PublicModelUse,
    PublicOperationsResponse,
    PublicOrphanedSlot,
    PublicProvisionalScore,
    PublicRolloutQueueEntry,
    PublicRunModels,
    PublicScreenerHeartbeat,
    PublicScreenerHeartbeatsResponse,
    PublicScreenerProgress,
    PublicScreenerWatchdogResponse,
    PublicScreeningAttempt,
    PublicScreeningDispute,
    PublicScreeningReviewEvidence,
    PublicScreeningReviewFinding,
    PublicScreeningReviewLocation,
    PublicSubmissionFamily,
    PublicSubmissionFamilyMember,
    PublicSubmissionImageBuild,
    PublicSubmissionImageBuildSnapshot,
    PublicSubmissionPipeline,
    PublicSubmissionScores,
    PublicSubmissionsResponse,
    PublicSubmissionSummary,
    PublicSystemMetrics,
    PublicTokenEfficiency,
    PublicTokenUsage,
    PublicV9AuthoritativeToolGate,
    PublicV9BaseEvidence,
    PublicV9ModelUseGate,
    PublicV9ScoreGateEvidence,
    PublicValidationAttempt,
    PublicValidatorHeartbeat,
    PublicValidatorHeartbeatsResponse,
    PublicValidatorName,
    PublicValidatorNamesResponse,
    PublicValidatorScore,
    PublicValidatorSlotPolicy,
    PublicValidatorWeightVector,
)
from ditto.api_models import bench_glossary as bench_glossary_data
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_progress import BenchmarkProgressStage
from ditto.api_models.model_use import ModelUseVerdict
from ditto.api_models.public import (
    BenchServiceability,
    FleetAvailability,
    FleetHealth,
    ScorerLiveness,
    ValidatorAssignmentState,
)
from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    ScreenerProgress,
    ScreenerRuntimeState,
    ScreenEvidenceItem,
    SourceReviewFinding,
)
from ditto.api_models.stack_health import ValidatorStackHealth
from ditto.api_models.system_health import SystemMetrics
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator import (
    V9BaseEvidence,
    V9ConfirmationReceipt,
    ValidatorRuntimeState,
)
from ditto.api_models.validator_capabilities import (
    ValidatorCapabilities,
    ValidatorStackIdentity,
)
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.bench import CURRENT_BENCH_VERSION, is_bench_version_retired
from ditto.api_server.benchmark_rollout import rolling_qualification_blockers
from ditto.api_server.continual_retest_settings import (
    aggregate_is_active,
    tie_weighting_is_active,
)
from ditto.api_server.datapipeline import DataPipelineError
from ditto.api_server.efficiency import (
    EfficiencyBoardView,
    ensure_efficiency_state,
    preview_efficiency_board,
    read_efficiency_board,
)
from ditto.api_server.endpoints.scoring import (
    _BOUNDED_EFFICIENCY_FACTOR_PROTOCOL,
    _confirmation_composites,
    _confirmation_seeds,
    _fleet_safe_efficiency_adjustments,
    _ledger_stderr,
)
from ditto.api_server.endpoints.screener import GeneratorDep
from ditto.api_server.endpoints.upload import _verify_signature
from ditto.api_server.endpoints.validator import SessionDep, StorageDep
from ditto.api_server.koth import (
    KOTH_BAND_DECAY_MIN_BENCH_VERSION,
    KOTH_BAND_DECAY_RATE,
    KOTH_BAND_DECAY_START_COMPOSITE,
    KOTH_CHAMPION_SHARE,
    KOTH_DETHRONE_Z,
    KOTH_MARGIN,
    KOTH_RANK_SHARES,
    KOTH_TAIL_SIZE,
    KothEntry,
    bounded_efficiency_adjusted_quality,
    champion_defense,
    emission_allocation,
    project_koth,
)
from ditto.api_server.model_use import model_use_factor, model_use_policy
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.api_server.validator_slot_settings import (
    HostResourceSample,
    allowed_slot_count,
    resolve_slot_settings,
    validator_issuance_paused,
)
from ditto.chain import ChainError
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    BenchmarkDataset,
    BenchmarkRollout,
    ConfirmationScore,
    InferenceGrant,
    Score,
    ScreenerCapacitySnapshot,
    ScreenerNode,
    ScreeningDispute,
    ScreeningQuarantine,
    SubmissionImageBuild,
    ValidatorTicket,
)
from ditto.db.queries.agents import (
    get_public_activity_by_id,
    list_public_activity,  # noqa: F401 - legacy test seam for targeted deep links
    query_public_activity_page,
)
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_PUBLIC_ARTIFACT,
    record_artifact_fetch,
)
from ditto.db.queries.artifact_release import (
    ArtifactScoreQuorum,
    available_public_source_agent_ids,
    list_first_score_quorums,
)
from ditto.db.queries.artifact_release_settings import (
    ArtifactReleasePolicy,
    artifact_release_policy,
)
from ditto.db.queries.audit import GENESIS_HASH, list_audit_entries
from ditto.db.queries.benchmark_admission import (
    agent_is_admitted,
)
from ditto.db.queries.benchmark_rollout import (
    LEGACY_BENCH_VERSION,
    active_bench_version,
    open_rollout,
    protocol_serves_version,
    rollout_state,
    verified_scorer_for_version,
)
from ditto.db.queries.confirmation_bundles import (
    ActiveConfirmationWork,
    list_active_confirmation_work,
)
from ditto.db.queries.confirmation_scores import (
    DEFAULT_WAVE_MEMBERSHIP,
    WaveMembership,
    confirmation_composites_by_seed,
    confirmation_depths,
)
from ditto.db.queries.desired_era_backlog import prev_generation_agent_ids
from ditto.db.queries.heartbeats import (
    ActiveValidatorAssignment,
    ActiveValidatorWork,
    list_active_validator_assignments,
    list_active_validator_work,
    list_screener_heartbeats,
    list_validator_heartbeats,
    live_validator_fleet_supports_protocol,
)
from ditto.db.queries.inference import USAGE_ACCOUNTING_VERSION
from ditto.db.queries.king_reign import KingReveal, get_king_reveal
from ditto.db.queries.orphaned_leases import OrphanedLease, list_orphaned_leases
from ditto.db.queries.queue_order import (
    QueueGate,
    QueuePreviewEntry,
    preview_queue_order,
)
from ditto.db.queries.retirement import retired_agent_ids
from ditto.db.queries.retry_state import (
    AgentRetryState,
    classify_agent_retry_states,
    resolve_bench_version,
)
from ditto.db.queries.score_ranking import (
    completed_wave_data,
    dedupe_owner_rows,
    efficiency_tiebreak_composites,
    official_composites,
    resolve_ranking_scores,
)
from ditto.db.queries.scores import (
    SCORING_QUORUM,
    LedgerFamilyMember,
    LedgerRow,
    SubmissionRow,
    V9ConfirmationPublicProjection,
    attested_emission_owner_roots,
    emission_owner,
    get_public_health,
    get_score_counts,
    get_submission_scores,
    list_eligible_ledger,
    list_memory_leader_timeline,
    list_provisional_ledger,
    list_public_submissions,
    list_scored_bench_versions,
    list_scores_for_bench_version,
    list_submission_family_members,
    quorum_composites,
    v9_confirmation_policy_mode,
    v9_confirmation_public_projections,
)
from ditto.db.queries.screening import (
    get_running_screening_attempts,
    list_screening_attempts,
)
from ditto.db.queries.tickets import (
    get_score_continuation_floor,
    get_score_continuation_floor_row,
    get_score_priority_floors,
)
from ditto.score_order import score_order_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public"])

# The ledger only moves when a sweep records a new best score, so a short shared
# cache is safe and shields the DB from dashboard/CDN traffic.
#
# stale-while-revalidate is what makes a reload feel instant: past max-age the
# client may paint the cached body immediately and refresh it in the background,
# instead of showing empty panels for a round trip. The window is deliberately
# short here, so a stale board is never more than a couple of minutes old.
_CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=120"
_OPERATIONS_CACHE_CONTROL = "public, max-age=5, stale-while-revalidate=30"
_SUBMISSION_BUILD_WINDOW = timedelta(hours=24)
_SUBMISSION_BUILD_LIMIT = 8
_TIMELINE_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=3600"
# v1 predates the memory subscore, so it has nothing to plot on this axis.
_TIMELINE_MIN_BENCH_VERSION = 2
# Keep the complete scored memory era available to consumers without letting the
# per-version ledger scan grow without bound. The dashboard may apply a tighter
# presentation window to preserve legibility, but the public response retains v2
# through the current v9 rollout so a consumer never has to invent missing release
# metadata.
_TIMELINE_MAX_RELEASES = 8
# A leaderboard pinned to a *settled* benchmark version is finished work: the
# rollout has moved past it, so nothing routine writes to it again. It is still
# not immutable (an ATH review or a score replacement can correct an old row),
# so this is a long freshness window rather than `immutable`: a reload reuses it
# with no request at all, and any correction lands within the hour.
_SETTLED_BENCH_CACHE_CONTROL = "public, max-age=3600, stale-while-revalidate=86400"
_ARTIFACT_DOWNLOAD_TTL_SECONDS = 5 * 60
# One Pylon metagraph read, behind the 15s cache below, so this fires at most
# once every 15s no matter how many viewers are polling. 1.0s was too tight for
# a normal Pylon round trip under load and was the direct cause of a recurring
# "registration unknown" flash across every leaderboard row.
_REGISTRATION_LOOKUP_TIMEOUT_SECONDS = 3.0
_REGISTRATION_CACHE_TTL_SECONDS = 15.0
_REGISTRATION_FAILURE_CACHE_TTL_SECONDS = 5.0
# How long a *successful* registration mapping may keep being served after
# refreshes start failing. Registration only decorates the durable ledger, so a
# few minutes of slightly-old UIDs is far better for a reader than flipping the
# whole board to "unknown"; past this the mapping stops being evidence of
# anything current and the board does report unknown.
_REGISTRATION_MAX_STALE_SECONDS = 600.0
# `get_weights` opens a fresh substrate websocket and fully exhausts two storage
# maps; in prod that measured 10-21s, not the low seconds the old 4s budget
# assumed. Nearly every read was therefore killed before it could finish. Because
# PublicCacheMiddleware only stores 200s, each resulting 503 also left nothing
# cached, so the very next poll ran another doomed read: a self-sustaining
# failure loop that 503'd ~25% of requests to this endpoint, each 503 blanking
# the dashboard's chain-observation panel for that tick. The budget now exceeds
# the real cost of the read instead of guaranteeing it gets cancelled.
_CHAIN_WEIGHTS_TIMEOUT_SECONDS = 30.0
# The revealed matrix only changes when validators reveal, which is epoch scale
# (~72 min), so this is still far finer-grained than the data moves. Deliberately
# longer than the 30s max-age this endpoint declares: the response cache absorbs
# the polling, and this absorbs the response cache's own expiries.
_CHAIN_WEIGHTS_CACHE_TTL_SECONDS = 60.0
# Backoff after a failed refresh, so an upstream outage cannot put us back in the
# read-fail-retry-immediately loop described above.
_CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS = 30.0
# Ceiling on serving a cached matrix once refreshes fail. Past this the block it
# pins is too old to present even as explicitly stale, and the endpoint reverts
# to 503 rather than implying the chain state is roughly current.
_CHAIN_WEIGHTS_MAX_STALE_SECONDS = 1800.0
_TRANSCRIPT_MAX_BYTES = 32 << 20
_TRANSCRIPT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Historical reproduction must fail closed: only benchmark epochs whose exact
# generator release is known get a copyable command. Add a mapping deliberately
# when a future epoch pins its generator; never point an old score at ``latest``.


def _timeline_utc(value: datetime) -> datetime:
    """Normalize SQLite-naive and Postgres-aware rollout timestamps to UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# The exact generator release each benchmark version's reproduction commands
# pin. v0.8.0 is the tag cut from dittobench-datagen's anti-gaming branch at
# the v3 release (see dittobench-api docs/v3-release.md for the merge order).
_DATAGEN_VERSION_BY_BENCH_VERSION = {
    2: "v0.7.0",
    3: "v0.8.0",
    4: "v0.9.0",
    5: "v0.10.0",
    6: "v0.11.1",
    7: "v0.12.0",
}
# New generator releases live inside this monorepo and cannot reuse the retired
# repository's module tags. Add an exact semantic monorepo tag or 40-character
# commit here when a future benchmark epoch is activated; the public command
# will clone that immutable source and run the nested module in place.
_DATAGEN_MONOREPO_REF_BY_BENCH_VERSION: dict[int, str] = {}
# Generator releases from v0.8.0 on require an explicit `-bench-version`: the
# flag defaults to 0 and the binary exits 2 ("-bench-version is required")
# without it. protocol.CurrentBenchVersion is deliberately NOT a generation
# default -- it is display/release metadata only, so canonical generation must
# always be told its version explicitly.
#
# v0.7.0 predates the flag entirely and generates bench 2 unconditionally, so
# passing `-bench-version` to it fails with "flag provided but not defined".
# Bench 2 therefore keeps the flagless form.
_BENCH_VERSIONS_WITHOUT_VERSION_FLAG = frozenset({2})
_DATAGEN_RUN_SIZES = frozenset({"small", "medium", "full"})
_VALIDATOR_ONLINE_WINDOW = timedelta(minutes=5)
_VALIDATOR_STALE_WINDOW = timedelta(minutes=15)
_CONTINUAL_MEAN_PROTOCOL = 14
_TIE_WEIGHTING_PROTOCOL = 20
# Grace after a lease is issued before the validator is expected to report (in a
# heartbeat) that it has picked the agent up. Within this window an assigned-but-
# not-yet-reported validator reads as "assigning" rather than a mismatch, so the
# fleet view does not flap red during normal job hand-offs. A couple of heartbeat
# cycles is plenty; the lease is created when the validator itself claims work.
_ASSIGNMENT_HANDOFF_GRACE = timedelta(seconds=60)
# Pre-run stages that should complete in a couple of minutes (pull + start the
# screener-built image, generate the dataset). Sitting in one of them past the
# threshold means the run is wedged rather than progressing; running_benchmark is
# excluded because it can legitimately run to the validator's 75-minute cap.
_BENCHMARK_STALL_EARLY_STAGES: frozenset[BenchmarkProgressStage] = frozenset(
    {"preparing", "building_harness", "generating_dataset", "starting_harness"}
)
_BENCHMARK_STALL_AFTER = timedelta(minutes=15)
# Elapsed-time allowance a single completed check buys a ``running_benchmark``
# run before it reads as stalled. Deliberately an order of magnitude above the
# real per-check cost (v7 runs near 4s/check) so that only a genuinely frozen
# count trips it, never a slow-but-moving one. With the 15-minute startup grace
# a full 281-check v7 run may take ~4.8 hours before it is called stalled, well
# past the validator's own 75-minute cap — the signal is a floor on wedged runs,
# not a competing timeout.
_BENCHMARK_STALL_PER_CHECK = timedelta(seconds=60)
_PUBLIC_ACTIVITY_STATUSES = frozenset(
    {
        "waiting_screening",
        "screening",
        "waiting_validator",
        "evaluating",
        "below_score_floor",
        "not_queued",
        "retired",
        "under_review",
        "rejected",
        "scored",
        "live",
    }
)


def _error_detail(error: BaseException) -> str:
    """Render an exception so a message-less type still logs something usable.

    ``asyncio.wait_for`` / ``asyncio.timeout`` raise a bare ``TimeoutError()``
    with no args, so the obvious ``logger.warning("...: %s", error)`` renders the
    empty string. That is how the two upstream-read warnings below ended up
    firing thousands of times a day while saying nothing at all about why. Lead
    with the class name so a timeout is always distinguishable from a chain
    error, even when the exception itself carries no text.
    """
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


@dataclass(frozen=True)
class _RegistrationSnapshot:
    """A registration mapping plus the provenance needed to age it out.

    ``read_at`` is when the underlying chain read actually succeeded (not when
    this snapshot object was built), so a mapping re-armed after a failed refresh
    keeps reporting its true age. ``stale`` says the last refresh failed and this
    is a previous good read being served on.
    """

    expires_at: float
    uids_by_hotkey: dict[str, int] | None
    read_at: float
    stale: bool = False


@dataclass(frozen=True)
class _ChainWeightsSnapshot:
    """The last successfully read weight matrix, with its read time."""

    payload: PublicChainWeightsResponse
    read_at: float


def _chain_weights_lock(request: Request) -> asyncio.Lock:
    """Return the process-wide single-flight lock for the weight-matrix read.

    Created lazily on first use like the snapshot itself. The event loop is
    single-threaded, so there is no race between the check and the assignment.
    """
    lock = getattr(request.app.state, "public_chain_weights_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        request.app.state.public_chain_weights_lock = lock
    return lock


def _cached_chain_weights(request: Request) -> _ChainWeightsSnapshot | None:
    """Return the cached weight matrix, ignoring anything of an unexpected shape."""
    cached = getattr(request.app.state, "public_chain_weights", None)
    return cached if isinstance(cached, _ChainWeightsSnapshot) else None


def _chain_weights_payload(
    request: Request, snapshot: _ChainWeightsSnapshot
) -> PublicChainWeightsResponse:
    """Stamp the cached matrix with its age and whether refreshes are failing.

    ``stale`` means the most recent *attempt* to re-read the chain failed, which
    is the thing a reader actually needs to know. It is not merely "older than
    the TTL": a matrix a few seconds past its refresh window is being refreshed
    right now and is not worth flagging.
    """
    failed_at = getattr(request.app.state, "public_chain_weights_failed_at", None)
    return snapshot.payload.model_copy(
        update={
            "stale": isinstance(failed_at, float) and failed_at > snapshot.read_at,
            "age_seconds": round(max(0.0, time.monotonic() - snapshot.read_at), 1),
        }
    )


def _schedule_chain_weights_refresh(request: Request) -> None:
    """Kick off a background refresh, at most one at a time.

    Serve-while-revalidate. The cached matrix goes out on *this* request while
    the slow chain read happens off the request path. Refreshing inline instead
    would make whichever poller happened to arrive at TTL expiry wait out a
    multi-second read — and because PublicCacheMiddleware single-flights misses,
    every client that arrived during that window would wait with it.
    """
    lock = _chain_weights_lock(request)
    failed_at = getattr(request.app.state, "public_chain_weights_failed_at", None)
    recently_failed = (
        isinstance(failed_at, float)
        and time.monotonic() - failed_at < _CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS
    )
    if lock.locked() or recently_failed:
        return

    async def _refresh() -> None:
        async with lock:
            await _refresh_chain_weights(request)

    task = asyncio.create_task(_refresh())
    # asyncio only holds a weak reference to a running task, so an unreferenced
    # one can be garbage-collected mid-read. Keep it alive until it completes.
    tasks = getattr(request.app.state, "public_chain_weights_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        request.app.state.public_chain_weights_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _refresh_chain_weights(request: Request) -> _ChainWeightsSnapshot | None:
    """Read the matrix from chain and cache it, or return ``None`` on failure.

    Callers must hold :func:`_chain_weights_lock`; this is the only place that
    opens a substrate connection for this endpoint.
    """
    chain = getattr(request.app.state, "chain", None)
    config = getattr(request.app.state, "config", None)
    get_weights = getattr(chain, "get_weights", None)
    if chain is None or config is None or not callable(get_weights):
        return None
    try:
        snapshot = await asyncio.wait_for(
            get_weights(config.chain.netuid), timeout=_CHAIN_WEIGHTS_TIMEOUT_SECONDS
        )
    except (ChainError, TimeoutError) as error:
        request.app.state.public_chain_weights_failed_at = time.monotonic()
        logger.warning(
            "public chain weights refresh failed after %.1fs: %s",
            _CHAIN_WEIGHTS_TIMEOUT_SECONDS,
            _error_detail(error),
        )
        return None
    refreshed = _ChainWeightsSnapshot(
        payload=PublicChainWeightsResponse(
            generated_at=datetime.now(UTC),
            netuid=snapshot.netuid,
            block=snapshot.block,
            block_hash=snapshot.block_hash,
            owner_hotkey=snapshot.owner_hotkey,
            vectors=[
                PublicValidatorWeightVector(
                    validator_uid=vector.validator_uid,
                    validator_hotkey=vector.validator_hotkey,
                    weights=[
                        PublicChainWeight(
                            uid=weight.uid, hotkey=weight.hotkey, value=weight.value
                        )
                        for weight in vector.weights
                    ],
                )
                for vector in snapshot.vectors
            ],
        ),
        read_at=time.monotonic(),
    )
    request.app.state.public_chain_weights = refreshed
    return refreshed


@router.get("/weights", response_model=PublicChainWeightsResponse)
async def chain_weights(
    request: Request, response: Response
) -> PublicChainWeightsResponse:
    """Return the latest publicly revealed SN118 validator weight matrix.

    This reads Subtensor storage directly. With commit-reveal enabled the matrix
    is necessarily the last revealed state and may lag encrypted commitments;
    it is evidence of what is public on chain, not a substitute for Yuma's
    stake-weighted emissions calculation.

    The read is cached and refreshed in the background rather than run per
    request. It used to be inline with a budget (4s) well below what the read
    actually costs (10-21s), so it was usually cancelled; since the response
    cache in front of this endpoint stores only 200s, each resulting 503 left
    nothing cached and the next poll immediately retried the same doomed read.
    ~25% of requests 503'd, and every one of them blanked the dashboard's
    chain-observation panel for a tick — the reported "flickering".

    Now a served response never waits on chain: a cached matrix goes out
    immediately and any refresh happens off the request path. A failed refresh
    keeps serving the last known good matrix marked ``stale`` instead of
    returning nothing. A 503 is reserved for having genuinely never read the
    matrix, or having last read it so long ago that presenting it would
    misrepresent current chain state.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    cached = _cached_chain_weights(request)
    if cached is not None:
        age = time.monotonic() - cached.read_at
        if age >= _CHAIN_WEIGHTS_CACHE_TTL_SECONDS:
            _schedule_chain_weights_refresh(request)
        if age <= _CHAIN_WEIGHTS_MAX_STALE_SECONDS:
            return _chain_weights_payload(request, cached)

    # No usable cache, so this request does have to wait for a real read. Only
    # reachable on a cold process or after a very long outage.
    lock = _chain_weights_lock(request)
    async with lock:
        latest = _cached_chain_weights(request)
        if latest is not None and latest is not cached:
            # Refreshed by whoever held the lock ahead of us.
            return _chain_weights_payload(request, latest)
        failed_at = getattr(request.app.state, "public_chain_weights_failed_at", None)
        recently_failed = (
            isinstance(failed_at, float)
            and time.monotonic() - failed_at < _CHAIN_WEIGHTS_FAILURE_BACKOFF_SECONDS
        )
        refreshed = None if recently_failed else await _refresh_chain_weights(request)

    if refreshed is not None:
        return _chain_weights_payload(request, refreshed)
    raise HTTPException(status_code=503, detail="chain weights unavailable")


def screening_dispute_signing_message(agent_id: UUID, message: str) -> bytes:
    """Return the stable payload a miner signs to authorize one dispute."""

    digest = hashlib.sha256(message.encode()).hexdigest()
    return f"ditto-dispute-v1:{agent_id}:{digest}".encode()


def _public_dispute(dispute: ScreeningDispute) -> PublicScreeningDispute:
    return PublicScreeningDispute(
        status=dispute.status,  # type: ignore[arg-type]
        submitted_at=dispute.created_at,
        resolved_at=dispute.resolved_at,
        resolution=dispute.resolution,  # type: ignore[arg-type]
    )


def _public_terminal_screening_review(
    quarantine: ScreeningQuarantine | None,
    *,
    artifact_sha256: str,
) -> tuple[list[PublicScreeningReviewEvidence], PublicScreeningReviewFinding | None]:
    """Project only verified review data from a terminal cheating rejection.

    Active quarantines and release/rescreen resolutions retain their private source
    layout. The public projection strips evidence digests and the artifact digest,
    and never contains source snippets, prompts, transcripts, or challenge values.
    """
    if (
        quarantine is None
        or quarantine.status != "resolved"
        or quarantine.resolution != "reject"
    ):
        return [], None

    evidence: list[PublicScreeningReviewEvidence] = []
    if isinstance(quarantine.evidence, list):
        try:
            parsed = [
                ScreenEvidenceItem.model_validate(item)
                for item in quarantine.evidence[:16]
            ]
        except ValueError:
            parsed = []
        evidence = [
            PublicScreeningReviewEvidence(
                module=item.module_id,
                code=item.code,
                summary=item.summary,
            )
            for item in parsed
        ]

    if not isinstance(quarantine.finding, dict):
        return evidence, None
    try:
        finding = SourceReviewFinding.model_validate(quarantine.finding)
    except ValueError:
        return evidence, None
    if (
        quarantine.finding_digest is None
        or finding.canonical_digest() != quarantine.finding_digest
        or finding.artifact_sha256 != artifact_sha256
    ):
        return evidence, None
    return evidence, PublicScreeningReviewFinding(
        reviewer_revision=finding.prompt_revision,
        risk_level=finding.risk_level,
        confidence=finding.confidence,
        categories=sorted(set(finding.categories)),
        locations=[
            PublicScreeningReviewLocation(
                path=item.path,
                line=item.line,
                category=item.category,
            )
            for item in finding.evidence
        ],
        summary=finding.summary,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _public_artifact_release(
    *,
    status: AgentStatus,
    score_quorum: ArtifactScoreQuorum | None,
    policy: ArtifactReleasePolicy,
    king_reveal: KingReveal | None,
    now: datetime,
) -> PublicArtifactRelease:
    """Project source visibility without mutating a public GET request.

    Source release is **king-only** and gated by on-chain weights: an agent's
    source is revealed only once it has (1) held the KOTH crown and (2) had
    validators' revealed on-chain weights set on it (post commit-reveal). The
    embargo window is measured from that on-chain confirmation
    (``king_reveal.weight_confirmed_at``), not from the score quorum. An agent
    that touched the crown but is not yet chain-confirmed stays ``embargoed``
    with no unlock time; one that never reigned stays ``unavailable`` forever.

    Subnet policy short-circuits all of that. It is checked first, ahead of
    even the rejected/banned branch, because it is the one input whose answer
    does not depend on the submission: under ``never`` every submission is
    withheld identically, and checking it first means a future branch added
    above the ladder cannot accidentally publish one.
    """
    if not policy.releases_publicly:
        return PublicArtifactRelease(status="withheld", disclosure="never")

    if status in (AgentStatus.REJECTED, AgentStatus.BANNED):
        return PublicArtifactRelease(status="unavailable")

    finalized_at = score_quorum.finalized_at if score_quorum is not None else None
    first_crowned_at = king_reveal.first_crowned_at if king_reveal is not None else None
    weight_confirmed_at = (
        king_reveal.weight_confirmed_at if king_reveal is not None else None
    )
    available_at = (
        weight_confirmed_at + timedelta(hours=policy.embargo_hours)
        if weight_confirmed_at is not None
        else None
    )
    release_status: Literal[
        "awaiting_quorum", "under_review", "embargoed", "available", "unavailable"
    ]
    if status in (AgentStatus.ATH_PENDING_REVIEW, AgentStatus.QUARANTINED):
        release_status = "under_review"
    elif score_quorum is None:
        release_status = "awaiting_quorum"
    elif status not in (AgentStatus.SCORED, AgentStatus.LIVE):
        release_status = "unavailable"
    elif king_reveal is None:
        # Never held the crown: the source stays private (king-only release).
        release_status = "unavailable"
    elif available_at is None:
        # Ever king, but on-chain weights not yet confirmed: withheld with no
        # unlock time until commit-reveal confirms validators backed this miner.
        release_status = "embargoed"
    else:
        release_status = "available" if now >= available_at else "embargoed"

    return PublicArtifactRelease(
        status=release_status,
        bench_version=(
            score_quorum.bench_version if score_quorum is not None else None
        ),
        score_quorum=SCORING_QUORUM,
        embargo_hours=policy.embargo_hours,
        finalized_at=finalized_at,
        crowned_at=first_crowned_at,
        weight_confirmed_at=weight_confirmed_at,
        available_at=(
            available_at if release_status in ("embargoed", "available") else None
        ),
        download_available=release_status == "available",
    )


async def _artifact_release_snapshot(
    session: AsyncSession,
    *,
    statuses: dict[UUID, AgentStatus],
    now: datetime,
    policy: ArtifactReleasePolicy | None = None,
) -> dict[UUID, PublicArtifactRelease]:
    """Batch-load release metadata for a public response.

    Takes ``agent_id -> status`` rather than ORM rows so hot public endpoints
    can keep their narrow column selects instead of hydrating full ``Agent``
    entities (which carry embeddings and fingerprint sketches).
    """
    score_quorums = await list_first_score_quorums(
        session,
        agent_ids=list(statuses),
        quorum=SCORING_QUORUM,
    )
    king_reveals = await get_king_reveal(session, agent_ids=list(statuses))
    # One policy read for the whole batch: the setting is subnet-wide, so there
    # is nothing per-agent to look up and no per-agent column to join.
    policy = policy or await artifact_release_policy(session)
    return {
        agent_id: _public_artifact_release(
            status=status,
            score_quorum=score_quorums.get(agent_id),
            policy=policy,
            king_reveal=king_reveals.get(agent_id),
            now=now,
        )
        for agent_id, status in statuses.items()
    }


def _public_system_metrics(raw: dict | None) -> PublicSystemMetrics | None:
    """Validate stored telemetry again and expose only the fixed public allowlist."""
    if not isinstance(raw, dict):
        return None
    try:
        metrics = SystemMetrics.model_validate(raw)
    except Exception:  # noqa: BLE001 - malformed historical rows stay private
        return None
    return PublicSystemMetrics(
        cpu_percent=metrics.cpu_percent,
        memory_percent=metrics.memory_percent,
        disk_percent=metrics.disk_percent,
        docker_status=metrics.docker.status,
        running_containers=metrics.docker.running_containers,
        unhealthy_containers=metrics.docker.unhealthy_containers,
    )


def _screener_system_metrics(raw: dict | None) -> PublicSystemMetrics | None:
    """Read legacy raw metrics or the private v2 telemetry envelope."""
    if isinstance(raw, dict) and "screening_progress" in raw:
        nested = raw.get("system_metrics")
        return _public_system_metrics(nested if isinstance(nested, dict) else None)
    return _public_system_metrics(raw)


def _stored_screener_progress(raw: dict | None) -> ScreenerProgress | None:
    """Revalidate only the signed progress pair from a v2 storage envelope."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("screening_progress")
    if not isinstance(value, dict):
        return None
    try:
        return ScreenerProgress.model_validate(value)
    except Exception:  # noqa: BLE001 - malformed historical rows stay private
        return None


def _benchmark_stalled(
    stage: BenchmarkProgressStage | None,
    started_at: datetime,
    now: datetime,
    *,
    completed: int | None = None,
) -> bool:
    """Flag a run that has taken far longer than its own reported progress allows.

    Two independent shapes of "wedged", both derived from data the heartbeat
    already carries — this asks nothing new of the validator.

    **Early stages.** Pulling and starting the screener-built image plus
    generating the dataset take a couple of minutes, so a quarter hour still in
    one of them means the run is wedged (e.g. the sandbox executor cannot start
    the container).

    **``running_benchmark``.** Previously excluded outright, on the reasoning
    that the stage can legitimately run to the validator's 75-minute cap. That
    left the longest stage of the run — the one where a wedge is most likely and
    most expensive — with no stall signal at all: a benchmark frozen at 3/281
    read exactly like one steadily working through 281 checks. It is now judged
    against its *own* reported count rather than against wall-clock alone: each
    completed check buys ``_BENCHMARK_STALL_PER_CHECK`` of elapsed time on top of
    a fixed startup grace. A healthy run earns headroom far faster than it spends
    it (v7 sits near 4s/check against a 60s allowance), so a genuinely
    progressing benchmark is never mislabelled; a frozen one crosses the line and
    stays crossed.

    This is deliberately *not* the same question as "is the validator still
    reporting". A stalled run has a fresh heartbeat and a frozen count; a stalled
    *stream* has a stale ``seen_at`` and is surfaced as ``online``/
    ``heartbeat_stale`` by :func:`_fleet_classification`. Conflating the two is
    what makes an operator distrust the whole view, so they stay separate
    signals.
    """
    if stage in _BENCHMARK_STALL_EARLY_STAGES:
        return now - started_at >= _BENCHMARK_STALL_AFTER
    if stage != "running_benchmark" or completed is None:
        # No reported count is not evidence of a wedged run. An older protocol, or
        # a poll that degraded to unknown counts, leaves us with nothing to judge
        # the clock against, and a stall badge invented from missing telemetry is
        # exactly the false positive that teaches an operator to ignore it.
        return False
    earned = _BENCHMARK_STALL_AFTER + _BENCHMARK_STALL_PER_CHECK * max(completed, 0)
    return now - started_at >= earned


def _public_benchmark_progress(
    work: ActiveValidatorWork, now: datetime
) -> PublicBenchmarkProgress:
    """Project private signed counts onto the fixed public allowlist."""
    progress = work.progress
    started_at = cast(datetime, _aware(work.ticket.issued_at))
    if progress is None:
        return PublicBenchmarkProgress(
            agent_id=work.agent.agent_id,
            slot_id=work.ticket.slot_id,
            agent_name=work.agent.name,
            bench_version=work.ticket.bench_version,
            started_at=started_at,
        )
    percent: int | None = None
    completed_checks: int | None = None
    total_checks: int | None = None
    if progress.completed is not None and progress.total is not None:
        total_checks = progress.total
        completed_checks = (
            progress.total
            if progress.stage in {"finalizing", "submitting_result"}
            else progress.completed
        )
        # ``percent`` used to be quantized to the nearest 5%, to avoid "exposing
        # high-resolution timing". It never achieved that: ``completed_checks``
        # and ``total_checks`` sit beside it on this same model, both exact, and
        # any observer can divide one by the other. The quantizer only degraded
        # the progress bar — on a 281-check v7 run one bucket is ~14 checks, so a
        # smoothly advancing benchmark rendered as a bar that jumped every
        # ~55 seconds and otherwise looked frozen. Deriving it from the exact
        # counts discloses nothing they do not already disclose.
        #
        # The 100% ceiling is kept, and it is a UX rule rather than a privacy
        # one: a run is not "done" while it is still finalizing and signing, so
        # the bar must not sit at 100% with work outstanding. Only the terminal
        # stages are allowed to report it.
        exact = progress.completed * 100 // progress.total
        percent = (
            exact
            if progress.stage in {"finalizing", "submitting_result"}
            else min(99, exact)
        )
    return PublicBenchmarkProgress(
        agent_id=work.agent.agent_id,
        slot_id=work.ticket.slot_id,
        agent_name=work.agent.name,
        bench_version=work.ticket.bench_version,
        started_at=started_at,
        stage=progress.stage,
        completed_checks=completed_checks,
        total_checks=total_checks,
        percent=percent,
        stalled=_benchmark_stalled(
            progress.stage, started_at, now, completed=progress.completed
        ),
    )


def _fleet_classification(
    *, state: str, seen_at: datetime, now: datetime, metrics: PublicSystemMetrics | None
) -> tuple[bool, FleetAvailability, FleetHealth]:
    """Return online, availability, and health without treating omission as outage."""
    online = seen_at >= now - _VALIDATOR_ONLINE_WINDOW
    availability: FleetAvailability
    if online and state == "paused":
        availability = "paused"
    elif online:
        availability = "available"
    elif seen_at >= now - _VALIDATOR_STALE_WINDOW:
        availability = "stale"
    else:
        availability = "offline"

    health: FleetHealth
    if state == "error":
        health = "warning"
    elif metrics is None:
        health = "unknown"
    elif (
        metrics.memory_percent >= 90
        or metrics.disk_percent >= 95
        or metrics.docker_status == "degraded"
    ):
        health = "warning"
    elif metrics.docker_status == "unavailable":
        health = "unknown"
    else:
        health = "healthy"
    return online, availability, health


# A required stack component observed in one of these states means the validator
# cannot reliably do its job even when host metrics look fine (e.g. a scorer that
# is reachable but cannot reach its model relay). "unknown" is NOT here: it means
# the component was not observed (startup, mock, or an unconfigured probe), which
# must not raise a false warning.
_STACK_HEALTH_WARNING_STATES = frozenset(
    {"degraded", "unreachable", "identity_mismatch"}
)


def _stack_component_issues(stack_health: ValidatorStackHealth | None) -> list[str]:
    """Per-component labels for every required component that is not healthy.

    e.g. ``["dittobench_api: degraded", "model_relay: unreachable"]``. This is the
    detail behind a fleet ``warning``: the rollup badge stays a single word, but
    these labels name exactly which component and state caused it so the UI can
    show them (e.g. a badge tooltip) without hiding the reason or crowding the
    view. Empty when nothing is wrong.
    """
    if stack_health is None:
        return []
    return [
        f"{name}: {component.health}"
        for name in type(stack_health).model_fields
        for component in (getattr(stack_health, name),)
        if component is not None
        and component.required
        and component.health in _STACK_HEALTH_WARNING_STATES
    ]


# Heartbeat protocol that first carries scorer probe evidence. Below it,
# "unreported" is expected and says nothing about the scorer; at or above it,
# "unreported" means a validator that could have reported evidence and did not.
_SCORER_PROBE_PROTOCOL = 15

_SCORER_PROBE_LIVENESS: dict[str, ScorerLiveness] = {
    "served": "serving",
    "served_degraded": "degraded",
    "http_error": "not_serving",
    "unreadable": "not_serving",
    "timeout": "not_serving",
    "connect_error": "not_serving",
    "not_probed": "unreported",
}


def _scorer_liveness(
    capabilities: ValidatorCapabilities | None, protocol_version: int
) -> tuple[ScorerLiveness, list[str]]:
    """Return whether the scorer is serving, and the labels explaining it.

    A running container is not a serving scorer. The heartbeat already reported
    what the validator concluded about its scorer; this reads what the probe
    observed, so a sidecar that 404s its capability route stops looking like an
    old-but-fine v2 scorer, and a scorer whose reply was only partly readable
    stops looking fully healthy.
    """
    scorer = capabilities.scorer_benchmarks if capabilities is not None else None
    probe = scorer.probe if scorer is not None else None
    if probe is None:
        if protocol_version >= _SCORER_PROBE_PROTOCOL:
            return "unreported", ["scorer liveness not reported"]
        return "unreported", []
    liveness = _SCORER_PROBE_LIVENESS.get(probe.outcome, "unreported")
    if liveness == "serving":
        return liveness, []
    detail = (
        f"http {probe.http_status}"
        if probe.outcome == "http_error"
        else (probe.reason or probe.outcome)
    )
    if liveness == "unreported":
        return liveness, ["scorer was not probed"]
    label = "scorer not serving" if liveness == "not_serving" else "scorer degraded"
    if probe.consecutive_failures > 1:
        return liveness, [f"{label}: {detail} ({probe.consecutive_failures} in a row)"]
    return liveness, [f"{label}: {detail}"]


def _bench_serviceability(
    row: Any, *, active_bench_version: int
) -> BenchServiceability:
    """Can this validator serve the benchmark being scored, and if not, why?

    Exactly the gate ticket issuance applies, minus liveness: capability is a
    property of the stack, so a validator that merely went quiet keeps whatever
    verdict its last report earned. The legacy era is exempt here for the same
    reason it is exempt there — below the legacy floor the platform asks for no
    capability advertisement, so nobody is gated out.
    """
    if active_bench_version <= LEGACY_BENCH_VERSION:
        return "serving"
    if verified_scorer_for_version(row, version=active_bench_version) is not None:
        return "serving"
    if not protocol_serves_version(row.protocol_version, version=active_bench_version):
        # The distinction that matters operationally: this one cannot be fixed
        # from the scorer side at all, and it is permanent until someone
        # upgrades the validator.
        return "software_obsolete"
    return "scorer_unverified"


def _bench_serviceability_reason(
    serviceability: BenchServiceability, *, version: int, protocol_version: int
) -> str | None:
    """The tooltip label naming why a validator is earning nothing."""
    if serviceability == "software_obsolete":
        return (
            f"software too old for bench v{version} "
            f"(heartbeat protocol {protocol_version})"
        )
    if serviceability == "scorer_unverified":
        return f"scorer identity is not eligible for bench v{version}"
    return None


def _health_reasons(
    *,
    state: str,
    metrics: PublicSystemMetrics | None,
    active_benchmark: PublicBenchmarkProgress | None,
    stack_health: ValidatorStackHealth | None,
    scorer_reasons: list[str],
    bench_reason: str | None = None,
) -> list[str]:
    """Human-readable labels explaining a non-healthy fleet badge.

    Mirrors the same conditions ``_fleet_classification`` and the entry rollup use
    for ``health``, so the badge never says ``warning``/``unknown`` without the
    payload also carrying exactly why. Empty for a fully healthy validator.
    Intended for a badge tooltip: detailed, but off the main view.
    """
    reasons: list[str] = []
    if bench_reason is not None:
        # First, because it is the reason the validator earns nothing: every other
        # label describes a validator that is at least in the running.
        reasons.append(bench_reason)
    if state == "error":
        reasons.append("worker reported an error state")
    if metrics is None:
        reasons.append("host metrics not reported")
    else:
        if metrics.memory_percent >= 90:
            reasons.append(f"memory {metrics.memory_percent}%")
        if metrics.disk_percent >= 95:
            reasons.append(f"disk {metrics.disk_percent}%")
        if metrics.docker_status == "degraded":
            reasons.append("docker degraded")
        elif metrics.docker_status == "unavailable":
            reasons.append("docker unavailable")
    if active_benchmark is not None and active_benchmark.stalled:
        reasons.append("benchmark stalled")
    reasons.extend(_stack_component_issues(stack_health))
    reasons.extend(scorer_reasons)
    return reasons


def _safe_models(details: dict) -> PublicRunModels | None:
    """Pull the run's models from the details blob, tolerating a malformed shape."""
    raw = details.get("models")
    if not isinstance(raw, dict):
        return None
    try:
        return PublicRunModels.model_validate(raw)
    except Exception:  # noqa: BLE001 - a bad blob must not break the leaderboard
        return None


_TRANSCRIPT_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _safe_transcript_sha256(details: dict) -> str | None:
    """Pull the score's declared transcript digest, tolerating malformed blobs."""
    raw = details.get("transcript_sha256")
    if isinstance(raw, str) and _TRANSCRIPT_SHA256_HEX.fullmatch(raw):
        return raw
    return None


def _safe_transform_robustness(details: dict) -> tuple[float | None, int | None]:
    """Pull the reproduce-under-transform audit result, tolerating bad blobs.

    Returns ``(None, None)`` for a run that carried no audit pairs or predates
    the audit, so an absent metric is never published as a failing one.
    """
    raw = details.get("transform_robustness")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None, None
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        return None, None
    pairs = details.get("audit_case_count")
    if not isinstance(pairs, int) or isinstance(pairs, bool) or pairs < 0:
        pairs = None
    return value, pairs


def _safe_categories(details: dict) -> list[PublicCategoryStat] | None:
    """Pull the per-category breakdown, dropping any malformed entries."""
    raw = details.get("per_category")
    if not isinstance(raw, list):
        return None
    out: list[PublicCategoryStat] = []
    for c in raw:
        try:
            out.append(PublicCategoryStat.model_validate(c))
        except Exception:  # noqa: BLE001 - skip a bad category, keep the rest
            continue
    return out or None


def _safe_integrity(details: dict) -> PublicBenchIntegrity | None:
    """Assemble the anti-overfit / integrity telemetry from the details blob.

    The scoring engine nests these under ``paraphrase`` / ``lexical_gap`` sub-dicts
    plus flat ``capped_tool_cases`` / ``seeding_waves``; flatten defensively so a
    partial or malformed shape yields ``None`` fields, never an error."""
    para = details.get("paraphrase")
    para = para if isinstance(para, dict) else {}
    lex = details.get("lexical_gap")
    lex = lex if isinstance(lex, dict) else {}

    def _i(v: object) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def _f(v: object) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)

    try:
        model = PublicBenchIntegrity(
            paraphrase_applied=_i(para.get("applied")),
            paraphrase_attempted=_i(para.get("attempted")),
            paraphrase_fallback=_i(para.get("fallback")),
            lexical_gap_rewritten=_i(lex.get("rewritten")),
            lexical_gap_questions=_i(lex.get("questions")),
            lexical_gap_mean_before=_f(lex.get("mean_before")),
            lexical_gap_mean_after=_f(lex.get("mean_after")),
            capped_tool_cases=_i(details.get("capped_tool_cases")),
            seeding_waves=_i(details.get("seeding_waves")),
        )
    except Exception:  # noqa: BLE001 - a bad blob must not break the leaderboard
        return None
    if all(v is None for v in model.model_dump().values()):
        return None
    return model


# The closed vocabulary of scorer notes that may be published.
#
# ``details.per_case[].notes`` is free text emitted by the two Go scorers —
# ``dittobench-api/internal/scorer`` for tool cases and
# ``dittobench-datagen/grade`` for memory cases — and several of their format
# strings interpolate DATASET content: the memory grader embeds the distractor
# value it matched (``grade.go``), and the trajectory scorer embeds required and
# forbidden argument names (``trajectory.go``). Forwarding those verbatim leaks
# answer-key material on an unauthenticated, CDN-cacheable endpoint, and on
# ``provisional_scores`` it does so *before* the ``/agent/{id}/dataset`` reveal
# gate that withholds answer keys until a run is finalized.
#
# So a published note is re-derived, never forwarded: it must either equal one of
# the constants below, or match a known template and be REBUILT from that
# template's validated primitive captures (an int, a two-decimal float, a bool,
# or an AnswerKind drawn from the closed enum). Anything else is dropped. This
# gives ``notes`` the same "safe by construction" property the rest of the
# projection already has — a scorer that grows a new note, or an existing note
# that starts interpolating a value, is withheld by default rather than leaked.
_PUBLIC_NOTE_CONSTANTS = frozenset(
    {
        # dittobench-datagen/grade/grade.go
        "empty response",
        "surfaced the forbidden value (scored 0)",
        "complied with prompt injection: emitted the payload (scored 0)",
        "cross-graph leak: surfaced another user's value (scored 0)",
        "surfaced the bait nonce, not the user's own (scored 0)",
        "abstained on an answerable question (scored 0)",
        # dittobench-api/internal/scorer/scorer.go
        "answer incorporated the served tool result",
        "answer did NOT incorporate the served tool result",
        "trajectory observed via tool_endpoint (authoritative)",
        "capped: observable case not executed via tool_endpoint "
        "(self-report untrusted)",
        "cross-graph leak: response surfaced another user's value (scored 0)",
        "deterministic answer match (no judge call)",
        "judge flagged prompt-injection attempt (case scored 0)",
        "no response from harness (error or timeout)",
        "memory request handled via memory retrieval (internal or memory tool)",
        "no memory retrieval attempted (no memory tool call and no answer)",
    }
)

# AnswerKind (``dittobench-datagen/protocol``): the closed set of deterministic
# grading checks. The grader interpolates this into three of its notes, so it is
# validated against the enum rather than echoed as free text — a dataset carrying
# a rogue AnswerKind cannot smuggle a value out through the verdict line.
_PUBLIC_NOTE_ANSWER_KINDS = frozenset(
    {
        "value",
        "number",
        "list",
        "ordered_list",
        "duration",
        "reversal",
        "persistence",
        "decline",
    }
)


def _note_answer_kind(match: re.Match[str], template: str) -> str | None:
    kind = match["kind"]
    return template.format(kind=kind) if kind in _PUBLIC_NOTE_ANSWER_KINDS else None


# Each entry is ``(pattern, rebuild)``. ``rebuild`` receives the full match and
# returns the note to publish, assembled from the captures, or ``None`` to drop.
# The stored bytes are never returned directly.
_PUBLIC_NOTE_TEMPLATES: tuple[
    tuple[re.Pattern[str], Callable[[re.Match[str]], str | None]], ...
] = (
    # Memory grader verdicts: mechanical, but carry the AnswerKind.
    (
        re.compile(r"deterministic (?P<kind>[a-z_]{1,16}) match"),
        lambda m: _note_answer_kind(m, "deterministic {kind} match"),
    ),
    (
        re.compile(r"no deterministic (?P<kind>[a-z_]{1,16}) match"),
        lambda m: _note_answer_kind(m, "no deterministic {kind} match"),
    ),
    (
        re.compile(r"partial (?P<kind>[a-z_]{1,16}) match \((?P<frac>\d\.\d{2})\)"),
        lambda m: (
            None
            if (kind := m["kind"]) not in _PUBLIC_NOTE_ANSWER_KINDS
            else f"partial {kind} match ({float(m['frac']):.2f})"
        ),
    ),
    # Trajectory / judge telemetry: counts and bools only.
    (
        re.compile(r"(?P<n>\d{1,6}) extra/unexpected tool call\(s\)"),
        lambda m: f"{int(m['n'])} extra/unexpected tool call(s)",
    ),
    (
        re.compile(r"expected no tools but harness called (?P<n>\d{1,6})"),
        lambda m: f"expected no tools but harness called {int(m['n'])}",
    ),
    (
        re.compile(r"judged correct=(?P<c>true|false) grounded=(?P<g>true|false)"),
        lambda m: f"judged correct={m['c']} grounded={m['g']}",
    ),
    # Value-bearing notes: the mechanical verdict is publishable, the value it
    # was rendered around is not. The distractor and the required/forbidden
    # argument names are dataset-derived (answer key); the misrouted tool name is
    # agent-supplied and therefore unbounded attacker-controlled text. All four
    # collapse to the verdict alone.
    (
        re.compile(r'surfaced a wrong same-attribute value ".*" \(scored 0\)'),
        lambda _: "surfaced a wrong same-attribute value (scored 0)",
    ),
    (
        re.compile(r"wrong value for arg .*"),
        lambda _: "wrong value for a required arg",
    ),
    (
        re.compile(r"forbidden arg present: .*"),
        lambda _: "forbidden arg present",
    ),
    (
        re.compile(r"misrouted a memory request to a non-memory tool: .*"),
        lambda _: "misrouted a memory request to a non-memory tool",
    ),
)


def _public_note(raw: object) -> str | None:
    """Re-derive one publishable note from a stored scorer note, or drop it."""
    if not isinstance(raw, str):
        return None
    note = raw.strip()
    if note in _PUBLIC_NOTE_CONSTANTS:
        # Set membership means the note is byte-identical to a constant declared
        # above, so returning it publishes our own string, not the stored one.
        return note
    for pattern, rebuild in _PUBLIC_NOTE_TEMPLATES:
        match = pattern.fullmatch(note)
        if match is not None:
            return rebuild(match)
    return None


def _public_notes(raw: object) -> list[str] | None:
    """Project a stored per-case ``notes`` list through the closed vocabulary."""
    if not isinstance(raw, list):
        return None
    clean = [note for note in map(_public_note, raw) if note is not None]
    return clean or None


def _safe_case_results(details: dict) -> list[PublicCaseResult] | None:
    """Redact ``details.per_case`` down to the publishable per-case view.

    Whitelists only ``category / kind / score / correct / latency_ms / notes``:
    the answer-key fields (``expected``, the agent's ``called`` tools, the
    seed-derived ``case_id``, and any other key) are dropped by construction, not
    filtered out, so a new per-case field can never leak by default. ``notes`` is
    held to the same standard by :func:`_public_notes`, which rebuilds each note
    from a closed vocabulary rather than forwarding the scorer's free text (which
    interpolates distractor values and argument names). ``None`` when there is no
    usable per-case data.
    """
    per_case = details.get("per_case")
    if not isinstance(per_case, list):
        return None
    out: list[PublicCaseResult] = []
    for c in per_case:
        if not isinstance(c, dict):
            continue
        score = c.get("score")
        category = c.get("category")
        if not isinstance(category, str):
            continue
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        kind = c.get("kind")
        latency = c.get("latency_ms")
        correct = c.get("correct")
        clean_notes = _public_notes(c.get("notes"))
        try:
            out.append(
                PublicCaseResult(
                    category=category,
                    kind=str(kind) if isinstance(kind, str) else "",
                    score=float(score),
                    correct=correct if isinstance(correct, bool) else None,
                    latency_ms=(
                        latency
                        if isinstance(latency, int) and not isinstance(latency, bool)
                        else None
                    ),
                    notes=clean_notes,
                )
            )
        except Exception:  # noqa: BLE001 - skip a bad case, keep the rest
            continue
    return out or None


def _safe_calibration(details: dict) -> tuple[float | None, int | None]:
    """Pull the advisory calibration telemetry (prod hardening P5): the mean
    Brier score over confidence-reporting cases and its sample size. Tolerates
    a malformed blob: anything out of range degrades to ``(None, None)``.
    Never scored; surfacing it costs nothing to harnesses that omit confidence.
    """
    brier = details.get("calibration_brier")
    n = details.get("calibration_n")
    if isinstance(brier, bool) or not isinstance(brier, (int, float)):
        return None, None
    b = float(brier)
    if not 0.0 <= b <= 1.0:
        return None, None
    count = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    return b, count


def _safe_token_usage(details: dict) -> PublicTokenUsage | None:
    raw = details.get("token_usage")
    if not isinstance(raw, dict):
        return None
    try:
        return PublicTokenUsage.model_validate(raw)
    except ValidationError:
        return None


def _safe_model_use(details: dict) -> PublicModelUse | None:
    """Project the stored model-use finding onto the public surface.

    Same shape as ``_safe_token_usage``: a malformed blob yields ``None``
    rather than a 500, because a projection bug must never take the
    leaderboard down.
    """
    raw = details.get("model_use")
    if not isinstance(raw, dict):
        return None
    try:
        return PublicModelUse.model_validate(raw)
    except ValidationError:
        return None


def _safe_v9_base(details: dict) -> V9BaseEvidence | None:
    """Read only a complete typed v9 root; malformed telemetry disappears."""

    raw = details.get("v9_base")
    if not isinstance(raw, dict):
        return None
    try:
        return V9BaseEvidence.model_validate(raw)
    except ValidationError:
        return None


def _safe_public_v9_base(details: dict) -> PublicV9BaseEvidence | None:
    """Project a valid signed v9 root onto the public dashboard allowlist."""

    evidence = _safe_v9_base(details)
    if evidence is None:
        return None
    gates = evidence.score_gates
    model = gates.model_use
    tool = gates.authoritative_tool
    return PublicV9BaseEvidence(
        bench_version=evidence.bench_version,
        score_gates=PublicV9ScoreGateEvidence(
            rollout_mode=gates.rollout_mode,
            model_use=PublicV9ModelUseGate(
                administered_cases=model.administered_cases,
                eligible_cases=model.eligible_cases,
                successful_inference_cases=model.successful_inference_cases,
                missing_inference_cases=model.missing_inference_cases,
                observed_requests=model.observed_requests,
                successful_requests=model.successful_requests,
                request_coverage_bps=model.request_coverage_bps,
                coverage_bps=model.coverage_bps,
                threshold_bps=model.threshold_bps,
                result=model.result,
                factor_bps=model.factor_bps,
            ),
            authoritative_tool=PublicV9AuthoritativeToolGate(
                expected_executions=tool.expected_executions,
                matched_executions=tool.matched_executions,
                missing_executions=tool.missing_executions,
                unexpected_executions=tool.unexpected_executions,
                observed_executions=tool.observed_executions,
                coverage_bps=tool.coverage_bps,
                threshold_bps=tool.threshold_bps,
                result=tool.result,
                factor_bps=tool.factor_bps,
            ),
        ),
    )


def _safe_token_efficiency(details: dict) -> PublicTokenEfficiency | None:
    raw = details.get("token_efficiency")
    if not isinstance(raw, dict):
        return None
    try:
        return PublicTokenEfficiency.model_validate(raw)
    except ValidationError:
        return None


def _safe_raw_composite(details: dict) -> float | None:
    raw = details.get("raw_composite")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


_QUALITY_FACTOR_SPECS = (
    (
        "tool_efficiency",
        "Tool efficiency",
        "Observed economy of tool use.",
        "tool_efficiency",
    ),
    (
        "metamorphic_consistency",
        "Consistency",
        "Share of equivalent prompts with consistent outcomes.",
        "metamorphic_consistency_factor",
    ),
    (
        "memory_over_call",
        "Memory over-call",
        "Avoidance of unnecessary memory-side actions.",
        "memory_over_call_factor",
    ),
    (
        "canary_integrity",
        "Canary integrity",
        "Whether the run avoided leaking the planted integrity canary.",
        "canary_integrity_factor",
    ),
    (
        "conversational_sanity",
        "Conversational sanity",
        "Aggregate performance across conversational behavior slices.",
        "conversational_sanity_factor",
    ),
    (
        "transform_robustness",
        "Transform robustness",
        "Consistency across reproducible transformed audit pairs.",
        "transform_audit_factor",
    ),
)


def _unit_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and 0.0 <= result <= 1.0 else None


def _safe_quality_factors(
    details: dict, aggregate_multiplier: float
) -> list[PublicBenchmarkQualityFactor]:
    """Allowlist scorer telemetry without deriving scorer policy curves."""
    explicit = details.get("benchmark_quality_factors")
    explicit = explicit if isinstance(explicit, dict) else {}
    out: list[PublicBenchmarkQualityFactor] = []
    known_product = 1.0
    known_count = 0
    _, audit_count = _safe_transform_robustness(details)
    for key, label, explanation, multiplier_key in _QUALITY_FACTOR_SPECS:
        metric = _unit_float(details.get(key))
        multiplier = _unit_float(explicit.get(key))
        if multiplier is None:
            multiplier = _unit_float(details.get(multiplier_key))
        # This field has always represented the exact applied factor; the other
        # observed metrics are not assumed to equal their versioned curves.
        if key == "tool_efficiency" and multiplier is None:
            multiplier = metric
        if metric is None and multiplier is None:
            continue
        if multiplier is not None:
            known_product *= multiplier
            known_count += 1
        out.append(
            PublicBenchmarkQualityFactor(
                key=key,
                label=label,
                metric=metric,
                multiplier=multiplier,
                audit_count=audit_count if key == "transform_robustness" else None,
                explanation=explanation,
            )
        )
    if known_count and known_product > 0:
        remainder = min(1.0, max(0.0, aggregate_multiplier / known_product))
        if remainder < 1.0 - 1e-6:
            out.append(
                PublicBenchmarkQualityFactor(
                    key="other_quality_effects",
                    label="Other quality effects",
                    multiplier=remainder,
                    explanation=(
                        "Combined effect not separable in this run's stored telemetry; "
                        "it reconciles the visible factors to the signed aggregate."
                    ),
                )
            )
    return out


def _composite_breakdown(
    *,
    tool_mean: float,
    memory_mean: float,
    final_composite: float,
    details: dict,
) -> PublicCompositeBreakdown | None:
    """Explain score arithmetic without reimplementing scorer gate policy.

    DittoBench owns the individual pre-token gates. The platform derives their
    combined multiplier from the scorer's signed pre-token composite, then
    publishes the independently recorded v5 token multiplier. That makes the
    full arithmetic auditable while keeping this API from drifting from the Go
    scorer or exposing answer-key-bearing case internals.
    """
    values = (tool_mean, memory_mean, final_composite)
    if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values):
        return None

    base = 0.5 * tool_mean + 0.5 * memory_mean
    token = _safe_token_efficiency(details)
    raw = _safe_raw_composite(details)
    if raw is None and token is not None:
        raw = token.raw_composite
    pre_token = raw if raw is not None else final_composite
    if base == 0.0:
        if pre_token != 0.0:
            return None
        quality_multiplier = 1.0
    else:
        # Every current gate is subtractive. Clamp only sub-micro rounding noise
        # from the scorer's six-decimal wire representation.
        quality_multiplier = min(1.0, max(0.0, pre_token / base))

    token_multiplier = token.multiplier if token is not None else None
    token_penalty = (
        min(0.1, max(0.0, 1.0 - token_multiplier))
        if token_multiplier is not None
        else None
    )
    try:
        return PublicCompositeBreakdown(
            base_accuracy=base,
            benchmark_quality_multiplier=quality_multiplier,
            quality_factors=_safe_quality_factors(details, quality_multiplier),
            pre_token_composite=pre_token,
            token_efficiency_multiplier=token_multiplier,
            token_penalty=token_penalty,
            maximum_token_penalty=(token.maximum_penalty if token else None),
            final_composite=final_composite,
        )
    except ValidationError:
        return None


def _public_entry(
    rank: int,
    r: LedgerRow,
    agent_name: str,
    agent_version: int | None,
    history: list[float] | None = None,
    *,
    finalized: bool = True,
    score_count: int = SCORING_QUORUM,
    registered: bool | None = None,
    miner_uid: int | None = None,
    fold_stderr: float | None = None,
    settled_composite: float | None = None,
    rollout_composite: float | None = None,
    rollout_score_count: int | None = None,
    artifact_release: PublicArtifactRelease | None = None,
    official_composite: float | None = None,
    pre_efficiency_composite: float | None = None,
    completed_wave_count: int = 0,
    initial_quorum_composites: tuple[float, ...] = (),
    completed_wave_composites: tuple[float, ...] = (),
    confirmation_seed_depth: int = 0,
    confirmation_seed_composites: tuple[float, ...] = (),
    continual_aggregate_active: bool = False,
    efficiency_bonus: float | None = None,
    efficiency_factor: float | None = None,
    efficiency_fold_applied: bool = False,
    efficiency_snapshot_id: UUID | None = None,
    efficiency_bonus_preview: float | None = None,
    efficiency_factor_preview: float | None = None,
    submission_family: PublicLeaderboardFamily | None = None,
    average_run_cost_microusd: int | None = None,
    inference_run_count: int = 0,
    v9_confirmation: V9ConfirmationPublicProjection | None = None,
) -> PublicLeaderboardEntry:
    """Map a ledger row to the public entry, exposing only the safe subset of
    ``details`` (never ``per_case``, which carries the answer key)."""
    details = r.details if isinstance(r.details, dict) else {}
    public_model_use = _safe_model_use(details)
    model_use_verdict = (
        ModelUseVerdict(public_model_use.verdict) if public_model_use else None
    )
    bench_version = r.bench_version
    if bench_version != 9:
        # Curve v3 is deliberately v9-only. Neutralize a mismatched row rather
        # than exposing or folding a factor under a legacy scoring contract.
        efficiency_factor = None
        efficiency_factor_preview = None
    v9_base = _safe_v9_base(details) if bench_version == 9 else None
    # A shadow gate is diagnostic, not score authority. Keep every v9 public
    # projection fail-closed until the signed root itself says the frozen gate
    # is in enforce mode; validators independently impose the same condition on
    # base-only v9 ledger rows.
    platform_model_use_factor = (
        1.0
        if bench_version == 9
        and v9_confirmation is not None
        and v9_confirmation.result_status == "full_confirmed"
        else (
            v9_base.applied_gate_factor_bps / 10_000
            if v9_base is not None and v9_base.score_gates.rollout_mode == "enforce"
            else 0.0
        )
        if bench_version == 9
        else model_use_factor(model_use_verdict, mode=model_use_policy().mode)
    )
    dataset_sha256 = details.get("dataset_sha256")
    raw_tokens = details.get("tokens")
    tokens = (
        raw_tokens
        if isinstance(raw_tokens, int) and not isinstance(raw_tokens, bool)
        else None
    )
    # A length-1 history is just the current score, not a trend; drop it so the
    # dashboard shows a sparkline only when there's an actual trajectory.
    trend = history if history and len(history) >= 2 else None
    calibration_brier, calibration_n = _safe_calibration(details)
    efficiency_base = (
        pre_efficiency_composite
        if pre_efficiency_composite is not None
        else official_composite
        if official_composite is not None
        else r.composite
    )
    effective_projection: float | None = None
    if efficiency_factor is not None:
        effective_projection = bounded_efficiency_adjusted_quality(
            efficiency_base * platform_model_use_factor,
            efficiency_factor,
        )
    elif efficiency_bonus is not None:
        # Preserve the frozen v1/v2 replay contract. Only curve v3 uses the
        # asymmetric downside / remaining-headroom transform.
        effective_projection = (
            efficiency_base * (1.0 + efficiency_bonus) * platform_model_use_factor
        )
    return PublicLeaderboardEntry(
        rank=(
            None
            if bench_version == 9
            and not finalized
            and (
                v9_confirmation is None
                or v9_confirmation.result_status != "full_confirmed"
            )
            else rank
        ),
        finalized=finalized,
        score_count=score_count,
        score_quorum=SCORING_QUORUM,
        agent_id=r.agent_id,
        agent_name=agent_name,
        agent_version=agent_version,
        artifact_release=artifact_release,
        submission_family=submission_family,
        miner_hotkey=r.miner_hotkey,
        miner_uid=miner_uid,
        registered=registered,
        emission_eligible=(
            finalized and r.eligible and registered if registered is not None else None
        ),
        composite=r.composite,
        official_composite=(
            official_composite if official_composite is not None else r.composite
        ),
        v9_confirmation_status=(
            v9_confirmation.result_status
            if v9_confirmation is not None
            else ("base_only" if bench_version == 9 else None)
        ),
        v9_full_confirmed_composite=(
            v9_confirmation.full_confirmed_composite
            if v9_confirmation is not None
            else None
        ),
        v9_confirmation_evidence_sha256=(
            v9_confirmation.evidence_sha256 if v9_confirmation is not None else None
        ),
        pre_efficiency_composite=(
            pre_efficiency_composite
            if pre_efficiency_composite is not None
            else (official_composite if official_composite is not None else r.composite)
        ),
        aggregate_method=(
            "continual_mean"
            if continual_aggregate_active and completed_wave_count > 0
            else "canonical_median"
        ),
        aggregate_sample_count=(
            SCORING_QUORUM + completed_wave_count
            if continual_aggregate_active
            else SCORING_QUORUM
        ),
        completed_wave_count=completed_wave_count,
        retained_sample_count=completed_wave_count,
        initial_quorum_composites=list(initial_quorum_composites),
        completed_wave_composites=list(completed_wave_composites),
        confirmation_seed_depth=confirmation_seed_depth,
        confirmation_seed_composites=list(confirmation_seed_composites),
        raw_composite=(
            float(details["raw_composite"])
            if isinstance(details.get("raw_composite"), (int, float))
            and not isinstance(details.get("raw_composite"), bool)
            else None
        ),
        efficiency_bonus=efficiency_bonus,
        efficiency_factor=efficiency_factor,
        efficiency_fold_applied=efficiency_fold_applied,
        # Full-confirmed v9 quality already includes its signature-bound model,
        # tool and semantic gates. Apply curve v3 after that authoritative
        # quality exactly once; legacy eras retain their historical model-use
        # reconciliation composition.
        effective_composite=effective_projection,
        efficiency_snapshot_id=efficiency_snapshot_id,
        # Deliberately NOT folded into effective_composite above: a preview is
        # arithmetic about a hypothetical, not a component of any score.
        efficiency_bonus_preview=efficiency_bonus_preview,
        efficiency_factor_preview=efficiency_factor_preview,
        # Use the exact uncertainty value sent to validators: a stashed re-score
        # SE when present, otherwise the k=3 quorum SEM. This keeps the displayed
        # band and the KOTH projection aligned with the real fold.
        composite_stderr=fold_stderr,
        calibration_brier=calibration_brier,
        calibration_n=calibration_n,
        tool_mean=r.tool_mean,
        memory_mean=r.memory_mean,
        first_seen=r.first_seen,
        # The fold's anchor, published beside the upload time rather than in
        # place of it. Both are true answers to different questions, and serving
        # only the upload time is what made a legitimately-held crown read as a
        # ranking bug to every miner comparing timestamps.
        crown_first_seen=r.crown_first_seen,
        median_ms=r.median_ms,
        n=r.n,
        eligible=r.eligible,
        bench_version=bench_version,
        settled_composite=settled_composite,
        rollout_composite=rollout_composite,
        rollout_score_count=rollout_score_count,
        dataset_sha256=dataset_sha256 if isinstance(dataset_sha256, str) else None,
        models=_safe_models(details),
        per_category=_safe_categories(details),
        integrity=_safe_integrity(details),
        tokens=tokens,
        token_usage=_safe_token_usage(details),
        model_use=public_model_use,
        token_efficiency=_safe_token_efficiency(details),
        composite_breakdown=_composite_breakdown(
            tool_mean=r.tool_mean,
            memory_mean=r.memory_mean,
            final_composite=r.composite,
            details=details,
        ),
        history=trend,
        case_results=_safe_case_results(details),
        average_run_cost_microusd=average_run_cost_microusd,
        inference_run_count=inference_run_count,
    )


def _public_submission_family(
    members: list[Any],
    *,
    representative_agent_id: UUID,
    selection_rule: Literal[
        "best_official_score_per_payment_owner",
        "best_canonical_score_per_payment_owner",
    ] = "best_official_score_per_payment_owner",
) -> PublicSubmissionFamily | None:
    """Project a payment-owner family without exposing its coldkey."""
    if not members:
        return None
    return PublicSubmissionFamily(
        member_count=len(members),
        selection_rule=selection_rule,
        members=[
            PublicSubmissionFamilyMember(
                agent_id=member.agent_id,
                agent_name=member.agent_name,
                agent_version=member.agent_version,
                miner_hotkey=member.miner_hotkey,
                canonical_composite=member.canonical_composite,
                submitted_at=member.submitted_at,
                representative=member.agent_id == representative_agent_id,
            )
            for member in members
        ],
    )


def _public_leaderboard_family(
    members: tuple[Any, ...],
    *,
    representative_agent_id: UUID,
) -> PublicLeaderboardFamily | None:
    """Project only the grouped children the leaderboard actually renders."""
    children = [
        PublicLeaderboardFamilyMember(
            agent_id=member.agent_id,
            agent_name=member.agent_name,
            agent_version=member.agent_version,
            canonical_composite=member.canonical_composite,
            submitted_at=member.submitted_at,
            miner_hotkey=member.miner_hotkey,
        )
        for member in members
        if member.agent_id != representative_agent_id
    ]
    return PublicLeaderboardFamily(members=children) if children else None


def _public_koth_emissions(
    rows: list[LedgerRow],
    *,
    stderrs: dict[UUID, float | None],
    quorum_by_agent: dict[UUID, list[float]] | None = None,
    confirmation_by_seed: dict[UUID, dict[int, float]] | None = None,
    confirmation_depth: dict[UUID, int] | None = None,
    include_continual_scores: bool = True,
    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
    anchor_version: int | None = None,
    efficiency_bonuses: dict[UUID, float] | None = None,
    efficiency_factors: dict[UUID, float] | None = None,
    tie_weighting_active: bool = False,
) -> PublicKothEmissions | None:
    """Project the caller's finalized, registration-eligible score pool."""
    quorum_values = quorum_by_agent or {}
    bonus_values = efficiency_bonuses or {}
    factor_values = efficiency_factors or {}
    candidates, by_seed, depths = completed_wave_data(
        rows,
        stderrs=stderrs,
        confirmation_by_seed=confirmation_by_seed,
        confirmation_depth=confirmation_depth,
        wave_membership=wave_membership,
        anchor_version=anchor_version,
    )

    fold_entries = []
    for raw_rank, row in enumerate(candidates, start=1):
        v9_confirmed = row.bench_version == 9 and row.v9_confirmation is not None
        details = row.details if isinstance(row.details, dict) else {}
        merged_confirmations: dict[int, float] = {}
        legacy_seeds = (
            _confirmation_seeds(details)
            if include_continual_scores and not v9_confirmed
            else None
        )
        legacy_composites = (
            _confirmation_composites(details)
            if include_continual_scores and not v9_confirmed
            else None
        )
        if legacy_seeds is not None and legacy_composites is not None:
            merged_confirmations.update(
                zip(legacy_seeds, legacy_composites, strict=False)
            )
        if not v9_confirmed:
            merged_confirmations.update(by_seed.get(row.agent_id, {}))
        confirmations = (
            sorted(merged_confirmations.items())
            if len(merged_confirmations) >= 2
            else None
        )
        fold_entries.append(
            KothEntry(
                miner_hotkey=row.miner_hotkey,
                agent_id=row.agent_id,
                composite=(
                    row.v9_confirmation["full_effective_micros"] / 1_000_000
                    if row.v9_confirmation is not None
                    else row.composite
                ),
                first_seen=row.fold_first_seen,
                raw_rank=raw_rank,
                bench_version=row.bench_version,
                composite_stderr=stderrs.get(row.agent_id),
                quorum_composites=(
                    () if v9_confirmed else tuple(quorum_values.get(row.agent_id, ()))
                ),
                completed_wave_composites=tuple(
                    value
                    for _seed, value in sorted(
                        ({} if v9_confirmed else by_seed.get(row.agent_id, {})).items()
                    )
                ),
                confirmation_composites=(
                    tuple(composite for _seed, composite in confirmations)
                    if confirmations is not None
                    else None
                ),
                confirmation_seeds=(
                    tuple(seed for seed, _composite in confirmations)
                    if confirmations is not None
                    else None
                ),
                efficiency_bonus=bonus_values.get(row.agent_id),
                efficiency_factor=factor_values.get(row.agent_id),
            )
        )

    projection = project_koth(fold_entries, distinct_hotkeys=tie_weighting_active)
    if projection is None:
        return None
    allocation = emission_allocation(
        fold_entries, projection, tie_pooling=tie_weighting_active
    )
    share_total = sum(allocation.shares)
    normalized_shares = tuple(share / share_total for share in allocation.shares)
    recipients = [
        PublicEmissionRecipient(
            role=(
                "joint_champion"
                if allocation.mode == "score_ceiling_pool"
                else "champion"
                if index == 0
                else "tail"
            ),
            agent_id=entry.agent_id,
            miner_hotkey=entry.miner_hotkey,
            raw_rank=entry.raw_rank,
            share_of_miner_pool=normalized_shares[index],
            shared_seed_confirmations=depths.get(entry.agent_id, 0),
        )
        for index, entry in enumerate(allocation.members)
    ]
    decision = projection.raw_leader_decision
    defense = champion_defense(fold_entries, projection)
    return PublicKothEmissions(
        margin=KOTH_MARGIN,
        dethrone_z=KOTH_DETHRONE_Z,
        band_decay_min_bench_version=KOTH_BAND_DECAY_MIN_BENCH_VERSION,
        band_decay_start_composite=KOTH_BAND_DECAY_START_COMPOSITE,
        band_decay_rate=KOTH_BAND_DECAY_RATE,
        champion_share=KOTH_CHAMPION_SHARE,
        rank_shares=KOTH_RANK_SHARES,
        tie_weighting_active=tie_weighting_active,
        tie_weighting_required_protocol=_TIE_WEIGHTING_PROTOCOL,
        allocation_mode=allocation.mode,
        score_ceiling_pool_size=(
            len(allocation.members) if allocation.mode == "score_ceiling_pool" else 0
        ),
        tail_size=KOTH_TAIL_SIZE,
        champion_agent_id=projection.champion.agent_id,
        champion_miner_hotkey=projection.champion.miner_hotkey,
        raw_leader_agent_id=projection.raw_leader.agent_id,
        raw_leader_miner_hotkey=projection.raw_leader.miner_hotkey,
        raw_leader_decision=(
            PublicDethroneDecision(
                challenger_lead=decision.challenger_lead,
                required_lead=decision.required_lead,
                margin_lead=decision.margin_lead,
                statistical_lead=decision.statistical_lead,
                method=decision.method,
                dethrones=decision.dethrones,
                required_score=decision.required_score,
                score_ceiling=decision.score_ceiling,
                ceiling_deadlocked=decision.ceiling_deadlocked,
            )
            if decision is not None
            else None
        ),
        champion_defense=(
            PublicDethroneDecision(
                challenger_lead=defense.challenger_lead,
                required_lead=defense.required_lead,
                margin_lead=defense.margin_lead,
                statistical_lead=defense.statistical_lead,
                method=defense.method,
                dethrones=defense.dethrones,
                required_score=defense.required_score,
                score_ceiling=defense.score_ceiling,
                ceiling_deadlocked=defense.ceiling_deadlocked,
            )
            if defense is not None
            else None
        ),
        recipients=recipients,
    )


@router.get("/bench/timeline", response_model=PublicBenchmarkTimelineResponse)
async def benchmark_timeline(
    response: Response,
    session: SessionDep,
) -> PublicBenchmarkTimelineResponse:
    """Running best finalized miner memory score across the newest contracts.

    The window follows the bench_version changelog rather than a hardcoded
    range, so shipping a new contract puts it on the timeline on its own. See
    :data:`_TIMELINE_MAX_RELEASES` for why it stays bounded.
    """

    response.headers["Cache-Control"] = _TIMELINE_CACHE_CONTROL
    # ``version_entries`` is newest-first, so the head of the list is the window.
    version_docs = [
        entry
        for entry in bench_glossary_data.version_entries()
        if int(entry["version"]) >= _TIMELINE_MIN_BENCH_VERSION
    ][:_TIMELINE_MAX_RELEASES]
    rollout_rows = (
        await session.execute(
            select(
                BenchmarkRollout.desired_version,
                BenchmarkRollout.created_at,
                BenchmarkRollout.activated_at,
            )
            .where(
                BenchmarkRollout.desired_version.in_(
                    [int(entry["version"]) for entry in version_docs]
                ),
                BenchmarkRollout.status.in_(
                    ("collecting", "blocked_ineligible", "activated")
                ),
            )
            .order_by(
                BenchmarkRollout.desired_version,
                BenchmarkRollout.created_at,
            )
        )
    ).all()
    # Prefer the activation record for settled contracts. An open contract has
    # no activation timestamp yet, so its latest collecting row supplies the
    # real rollout start instead of the changelog's fallback epoch.
    rollout_by_version: dict[int, Any] = {}
    for row in rollout_rows:
        version = int(row.desired_version)
        current = rollout_by_version.get(version)
        if (
            current is None
            or (current.activated_at is None and row.activated_at is not None)
            or (
                current.activated_at is None
                and row.activated_at is None
                and row.created_at > current.created_at
            )
        ):
            rollout_by_version[version] = row
    releases = [
        PublicBenchmarkRelease(
            bench_version=int(entry["version"]),
            released_at=(
                _timeline_utc(rollout_by_version[int(entry["version"])].created_at)
                if int(entry["version"]) in rollout_by_version
                else datetime.combine(
                    datetime.fromisoformat(str(entry["epoch"])).date(),
                    datetime_time.min,
                    tzinfo=UTC,
                )
            ),
            activated_at=(
                _timeline_utc(
                    cast(
                        datetime,
                        rollout_by_version[int(entry["version"])].activated_at,
                    )
                )
                if int(entry["version"]) in rollout_by_version
                and rollout_by_version[int(entry["version"])].activated_at is not None
                else None
            ),
            title=str(entry["title"]),
        )
        for entry in version_docs
    ]
    releases.sort(key=lambda entry: entry.bench_version)
    released_at = {entry.bench_version: entry.released_at for entry in releases}
    points = await list_memory_leader_timeline(
        session,
        bench_versions=[entry.bench_version for entry in releases],
        not_before_by_version=released_at,
    )
    return PublicBenchmarkTimelineResponse(
        generated_at=datetime.now(UTC),
        score_quorum=SCORING_QUORUM,
        releases=releases,
        points=[
            PublicBenchmarkTimelinePoint(
                recorded_at=point.recorded_at,
                bench_version=point.bench_version,
                agent_id=point.agent_id,
                agent_name=point.agent_name,
                miner_hotkey=point.miner_hotkey,
                memory_mean=point.memory_mean,
                composite=point.composite,
                score_count=point.score_count,
            )
            for point in points
        ],
    )


_LEADERBOARD_DETAIL_FIELDS = {
    "artifact_release",
    "initial_quorum_composites",
    "completed_wave_composites",
    "confirmation_seed_composites",
    "raw_composite",
    "efficiency_snapshot_id",
    "calibration_brier",
    "calibration_n",
    "dataset_sha256",
    "models",
    "per_category",
    "integrity",
    "tokens",
    "token_usage",
    "model_use",
    "token_efficiency",
    "composite_breakdown",
    "history",
    "case_results",
}


def _displayed_efficiency_factors(
    view: EfficiencyBoardView | None,
    finalized_by_id: dict[UUID, LedgerRow],
) -> dict[UUID, float]:
    """Return persisted v3 factors that are safe to expose as audit evidence.

    Display is intentionally independent of ``fold_enabled`` and protocol-19
    fleet readiness: operators need to inspect the frozen projection before
    activation. Authority remains separate—the caller passes this map through
    the fold/fleet gate before changing official ranking or KOTH.
    """
    if (
        view is None
        or view.preview
        or view.snapshot is None
        or view.snapshot.curve_version != 3
    ):
        return {}
    return {
        agent_id: float(assignment.factor)
        for agent_id, assignment in view.bonuses.items()
        if assignment.factor is not None
        and agent_id in finalized_by_id
        and finalized_by_id[agent_id].bench_version == 9
    }


@router.get(
    "/leaderboard",
    response_model=PublicLeaderboardResponse,
    response_model_exclude={"entries": {"__all__": _LEADERBOARD_DETAIL_FIELDS}},
)
async def leaderboard(
    request: Request,
    response: Response,
    session: SessionDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
) -> PublicLeaderboardResponse:
    """Best score per payment-time coldkey, with registration eligibility.

    The selected generation's hotkey remains the on-chain weight destination.
    Legacy rows without payment provenance fall back to one position per hotkey.
    """
    now = datetime.now(UTC)
    from ditto.db.queries.benchmark_rollout import open_rollout

    # Resolve the hot-swappable efficiency-bonus policy (latest append-only
    # revision overlaid on the env seed, short TTL) BEFORE ensure_efficiency_state
    # opens its own transaction on this session — the resolver reads on an
    # independent session so the request session stays pristine for that begin().
    # A backroom flip therefore lands on the next leaderboard read with no restart.
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    if efficiency_config.enabled and bench_version is None:
        # Materialize the current efficiency epoch (frozen cohort snapshot +
        # insert-once bonus rows) before any other read opens a transaction on
        # this session. A no-op below bench_version 7 and after the first call
        # of an epoch; failure degrades to serving the board without bonuses.
        try:
            await ensure_efficiency_state(
                session, efficiency_config, now=datetime.now(UTC)
            )
        except SQLAlchemyError:
            logger.warning(
                "efficiency bonus materialization failed; serving board without it",
                exc_info=True,
            )

    active_version = await active_bench_version(session)
    rollout = await open_rollout(session)
    desired_version = rollout.desired_version if rollout is not None else active_version
    display_version = bench_version or desired_version
    v9_confirmation_mode = await v9_confirmation_policy_mode(session)
    # A board explicitly pinned to a version the rollout has already moved past
    # is settled history; the default (unpinned) board and the versions still in
    # play keep the short live window. The dashboard's timeline fetches one board
    # per contract, so this is what makes a reload of that chart free.
    live_versions = {value for value in (active_version, desired_version) if value}
    # An empty live set means the rollout state is unknown; fall back to the
    # short window rather than let `all()` over nothing declare history settled.
    settled_bench_version = (
        bench_version is not None
        and bool(live_versions)
        and all(bench_version < value for value in live_versions)
    )
    response.headers["Cache-Control"] = (
        _SETTLED_BENCH_CACHE_CONTROL if settled_bench_version else _CACHE_CONTROL
    )
    ledger_rows = await list_eligible_ledger(
        session,
        include_fingerprints=False,
        include_details=False,
        bench_version=bench_version,
        owner_score="canonical" if bench_version is not None else "official",
        dedupe_owners=False,
    )
    # Enforce mode deliberately removes base-only/provisional v9 rows from the
    # authoritative ledger. Keep a separate, explicitly non-authoritative read
    # for public visibility: these rows are appended only to the provisional
    # section below, so they cannot rank, appear finalized, or earn emissions.
    v9_display_rows: list[LedgerRow] = []
    if display_version == 9 and v9_confirmation_mode == "enforce":
        authoritative_ids = {row.agent_id for row in ledger_rows}
        v9_display_rows = [
            replace(row, eligible=False)
            for row in await list_eligible_ledger(
                session,
                include_fingerprints=False,
                include_details=False,
                include_family_members=True,
                bench_version=9,
                owner_score="canonical",
                apply_v9_confirmation_policy=False,
            )
            if row.agent_id not in authoritative_ids
        ]
    visible_rows = ledger_rows + v9_display_rows
    selected_versions = {row.agent_id: row.bench_version for row in visible_rows}
    registration = await _current_registration(request)
    registered_uids = registration.uids_by_hotkey if registration else None
    registration_stale = registration is not None and registration.stale
    quorum = await quorum_composites(
        session,
        [row.agent_id for row in visible_rows],
        bench_versions=selected_versions,
    )
    fold_stderrs = {
        row.agent_id: (
            row.v9_confirmation["full_stderr_micros"] / 1_000_000
            if row.bench_version == 9 and row.v9_confirmation is not None
            else _ledger_stderr(
                (
                    {"composite_stderr": row.stored_composite_stderr}
                    if row.stored_composite_stderr is not None
                    else None
                ),
                quorum.get(row.agent_id, []),
            )
        )
        for row in visible_rows
    }
    score_counts = await get_score_counts(
        session,
        [row.agent_id for row in visible_rows],
        bench_versions=selected_versions,
    )
    finalized_rows = [
        row
        for row in ledger_rows
        if score_counts.get(row.agent_id, 0) >= SCORING_QUORUM
    ]
    finalized_ids = [row.agent_id for row in finalized_rows]
    fleet_protocol_ready = await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=_CONTINUAL_MEAN_PROTOCOL,
        bench_version=active_version,
        now=now,
        freshness=_VALIDATOR_STALE_WINDOW,
    )
    continual_settings = await request.app.state.continual_retest_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    continual_mean_active = bench_version is None and aggregate_is_active(
        continual_settings, fleet_protocol_ready=fleet_protocol_ready
    )
    tie_weighting_fleet_ready = await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=_TIE_WEIGHTING_PROTOCOL,
        bench_version=active_version,
        now=now,
        freshness=_VALIDATOR_STALE_WINDOW,
    )
    tie_weighting_active = bench_version is None and tie_weighting_is_active(
        continual_settings, fleet_protocol_ready=tie_weighting_fleet_ready
    )
    efficiency_view: EfficiencyBoardView | None = None
    if finalized_rows:
        board_version = max(row.bench_version for row in finalized_rows)
        try:
            if efficiency_config.enabled:
                efficiency_view = await read_efficiency_board(
                    session,
                    efficiency_config,
                    bench_version=board_version,
                    agent_ids=finalized_ids,
                    bench_versions=selected_versions,
                    now=datetime.now(UTC),
                    historical=settled_bench_version,
                )
            else:
                # Switched off, so show what the boost WOULD be rather than
                # hiding the block entirely. This computes and persists nothing;
                # it deliberately does not go via ensure_efficiency_state, whose
                # `enabled` gate is a gate on WRITES.
                efficiency_view = await preview_efficiency_board(
                    session,
                    efficiency_config,
                    bench_version=board_version,
                    now=datetime.now(UTC),
                )
        except SQLAlchemyError:
            logger.warning(
                "efficiency bonus read failed; serving board without it",
                exc_info=True,
            )
    if bench_version is None:
        confirmation_by_seed = await confirmation_composites_by_seed(
            session,
            agent_ids=finalized_ids,
            bench_version=active_version,
        )
        confirmation_depth = await confirmation_depths(
            session,
            agent_ids=finalized_ids,
            bench_version=active_version,
        )
    else:
        confirmation_by_seed = {}
        confirmation_depth = {}
    _, completed_by_seed, completed_depth = completed_wave_data(
        finalized_rows,
        stderrs=fold_stderrs,
        confirmation_by_seed=confirmation_by_seed,
        confirmation_depth=confirmation_depth,
        wave_membership=continual_settings.wave_membership,
        anchor_version=active_version,
    )
    pre_efficiency_composites = official_composites(
        finalized_rows,
        quorum=quorum,
        completed_waves=completed_by_seed,
        continual_mean_active=continual_mean_active,
    )
    efficiency_fold_active = bool(
        efficiency_config.enabled
        and efficiency_config.fold_enabled
        and efficiency_view is not None
        and not efficiency_view.preview
    )
    finalized_by_id = {row.agent_id: row for row in finalized_rows}
    efficiency_bonuses = (
        {
            agent_id: bonus_row.bonus
            for agent_id, bonus_row in efficiency_view.bonuses.items()
            if bonus_row.factor is None
            and efficiency_view.snapshot is not None
            and efficiency_view.snapshot.curve_version < 3
            and agent_id in finalized_by_id
        }
        if efficiency_fold_active and efficiency_view is not None
        else {}
    )
    # Persisted assignments remain public audit evidence before activation.
    # Keep this map separate from the factor map allowed to change official
    # ranking/KOTH: observing the frozen arithmetic must not require turning on
    # a consensus-affecting fold or waiting for fleet protocol readiness.
    displayed_efficiency_factors = _displayed_efficiency_factors(
        efficiency_view,
        finalized_by_id,
    )
    # A curve-v3 factor changes the exact-quality secondary order. Suppress it
    # from both the public tiebreak and validator-equivalent KOTH projection
    # until every recently-live validator capable of serving Bench v9 advertises
    # protocol 21, the first fold that consumes its quality-primary semantics.
    # Validators that cannot serve v9 are not part of this scoring contract and must not
    # indefinitely veto activation. Historical v1/v2 bonuses above deliberately
    # do not inherit this new gate.
    factor_fleet_ready = False
    if displayed_efficiency_factors and efficiency_fold_active:
        factor_fleet_ready = await live_validator_fleet_supports_protocol(
            session,
            minimum_protocol=_BOUNDED_EFFICIENCY_FACTOR_PROTOCOL,
            bench_version=9,
            now=now,
            freshness=_VALIDATOR_STALE_WINDOW,
        )
    efficiency_bonuses, efficiency_factors = _fleet_safe_efficiency_adjustments(
        efficiency_bonuses,
        displayed_efficiency_factors if efficiency_fold_active else {},
        factor_fleet_ready=factor_fleet_ready,
    )
    board_official_composites = official_composites(
        finalized_rows,
        quorum=quorum,
        completed_waves=completed_by_seed,
        continual_mean_active=continual_mean_active,
        efficiency_bonuses=efficiency_bonuses,
        efficiency_factors=efficiency_factors,
        efficiency_fold_active=efficiency_fold_active,
    )
    board_efficiency_tiebreaks = efficiency_tiebreak_composites(
        finalized_rows,
        official=board_official_composites,
        efficiency_factors=efficiency_factors,
    )
    finalized_rows = dedupe_owner_rows(
        finalized_rows,
        scores=board_official_composites,
        secondary_scores=board_efficiency_tiebreaks,
    )
    if finalized_rows:
        family_groups = await list_submission_family_members(
            session,
            bench_version=finalized_rows[0].bench_version,
        )
        family_by_agent = {
            member.agent_id: members
            for members in family_groups.values()
            for member in members
        }
        finalized_rows = [
            replace(
                row,
                family_members=tuple(
                    LedgerFamilyMember(
                        agent_id=member.agent_id,
                        agent_name=member.agent_name,
                        agent_version=member.agent_version,
                        canonical_composite=member.canonical_composite,
                        submitted_at=member.submitted_at,
                        miner_hotkey=member.miner_hotkey,
                    )
                    for member in family_by_agent.get(row.agent_id, [])
                ),
            )
            for row in finalized_rows
        ]
    # Match the validator's weight-authoritative population exactly: first keep
    # one representative per payment-time owner, then apply current registration
    # eligibility. Projecting emissions from the pre-deduplicated rows can crown
    # a generation that the public board has grouped under its representative,
    # leaving the visible leaderboard with no champion row at all.
    #
    # Durable scores still remain on the board after deregistration, but a
    # hotkey without a current neuron cannot be the KOTH champion or occupy a
    # participation-tail slot. When the chain snapshot is unavailable, keep the
    # deterministic score-only projection visible with unknown eligibility;
    # validators likewise preserve their last accepted weights until
    # registration can be read again.
    emission_rows = (
        finalized_rows
        if registered_uids is None
        else [row for row in finalized_rows if row.miner_hotkey in registered_uids]
    )
    # The factor-adjusted finalized board is now one row per owner, so the
    # provisional overlay suppresses and dedupes on that same owner graph.
    provisional_candidates = (
        [
            (row, score_counts.get(row.agent_id, 0))
            for row in ledger_rows
            if score_counts.get(row.agent_id, 0) < SCORING_QUORUM
        ]
        + [(row, score_counts.get(row.agent_id, 0)) for row in v9_display_rows]
        + list(await list_provisional_ledger(session, bench_version=bench_version))
    )
    # Pre-quorum rows have no continual mean, so the canonical comparator reads
    # their raw composite -- the same call ``list_provisional_ledger`` makes.
    provisional_candidates.sort(key=lambda candidate: score_order_key(candidate[0]))
    owner_rows = finalized_rows + [row for row, _count in provisional_candidates]
    owner_roots = await attested_emission_owner_roots(
        session,
        [
            (
                row.miner_hotkey,
                emission_owner(
                    miner_hotkey=row.miner_hotkey,
                    miner_coldkey=row.miner_coldkey,
                ),
            )
            for row in owner_rows
        ],
    )
    finalized_owners = set(owner_roots[: len(finalized_rows)])
    provisional_by_owner: dict[str, tuple[LedgerRow, int]] = {}
    for owner, candidate in zip(
        owner_roots[len(finalized_rows) :], provisional_candidates, strict=True
    ):
        if owner not in finalized_owners:
            provisional_by_owner.setdefault(owner, candidate)
    provisional_rows = list(provisional_by_owner.values())
    rows = finalized_rows + [row for row, _count in provisional_rows]
    v9_confirmations = await v9_confirmation_public_projections(
        session,
        agent_ids=[row.agent_id for row in rows if row.bench_version == 9],
    )
    # The run ledger is append-only for its retention window, and a grant never
    # records its own outcome: ``status`` tracks budget and revocation, so it is
    # ``exhausted`` both for a run that finished and for one a stalled validator
    # abandoned mid-flight. An elapsed ``ticket_deadline`` does not disambiguate
    # them either -- for a stuck validator the deadline passing is precisely the
    # evidence the run never completed. Averaging on either signal books partial
    # work as a whole run and drags the displayed mean down in proportion to how
    # many leases an agent has had abandoned, which is backwards: the agents the
    # fleet struggled on look cheapest.
    #
    # Completion is only knowable from the ticket that owned the lease, so join
    # it and take the leases whose validator actually posted a score. Matching
    # ``ticket_deadline`` to the ticket's current ``deadline`` keeps the lease
    # that produced the accepted score and drops the earlier abandoned attempts
    # of a retried ticket, which share the ticket row but not its deadline.
    #
    # Restrict to the current metering contract as well: a v1 grant charged the
    # unsettled tail a byte-length reservation worth roughly 4x the truth, and
    # ``InferenceGrant.usage_accounting_version`` exists precisely because those
    # totals cannot be compared across the meter change (there is no backfill --
    # what those calls really consumed was never recorded).
    run_costs: dict[tuple[UUID, int], tuple[int, int]] = {}
    if rows:
        cost_rows = (
            await session.execute(
                select(
                    InferenceGrant.agent_id,
                    InferenceGrant.bench_version,
                    func.avg(
                        InferenceGrant.cost_microusd
                        + InferenceGrant.embedding_cost_microusd
                    ),
                    func.count(),
                )
                .join(
                    ValidatorTicket,
                    and_(
                        InferenceGrant.agent_id == ValidatorTicket.agent_id,
                        InferenceGrant.bench_version == ValidatorTicket.bench_version,
                        InferenceGrant.validator_hotkey
                        == ValidatorTicket.validator_hotkey,
                        InferenceGrant.ticket_deadline == ValidatorTicket.deadline,
                    ),
                )
                .where(
                    InferenceGrant.agent_id.in_([row.agent_id for row in rows]),
                    ValidatorTicket.status == TicketStatus.SCORED,
                    InferenceGrant.usage_accounting_version == USAGE_ACCOUNTING_VERSION,
                    or_(
                        InferenceGrant.request_count > 0,
                        InferenceGrant.embedding_request_count > 0,
                    ),
                )
                .group_by(InferenceGrant.agent_id, InferenceGrant.bench_version)
            )
        ).tuples()
        run_costs = {
            (agent_id, version): (int(round(float(average))), int(count))
            for agent_id, version, average, count in cost_rows
            if average is not None
        }
    # During an open rollout the board is a mixed-version pool (v3 at quorum,
    # otherwise v2), which makes composite ordering jump between incomparable
    # scales. Expose each agent's settled active-version median (the comparable
    # ranking baseline) plus its desired-version settlement state (median so far
    # + accepted-score count) so the dashboard can rank by settled and show
    # rollout progress per row.
    rollout_states: dict[UUID, tuple[float | None, float | None, int | None]] = {}
    if rollout is not None and bench_version is None:
        board_ids = [row.agent_id for row in rows]
        settled_pools = await quorum_composites(
            session,
            board_ids,
            bench_versions=dict.fromkeys(board_ids, active_version),
        )
        desired_pools = await quorum_composites(
            session,
            board_ids,
            bench_versions=dict.fromkeys(board_ids, desired_version),
        )
        for board_agent_id in board_ids:
            settled_pool = settled_pools.get(board_agent_id, [])
            desired_pool = desired_pools.get(board_agent_id, [])
            rollout_states[board_agent_id] = (
                float(statistics.median(settled_pool))
                if len(settled_pool) >= SCORING_QUORUM
                else None,
                float(statistics.median(desired_pool)) if desired_pool else None,
                len(desired_pool),
            )
    agent_rows = (
        (
            await session.execute(
                select(Agent.agent_id, Agent.name, Agent.version).where(
                    Agent.agent_id.in_([row.agent_id for row in rows])
                )
            )
        )
        .tuples()
        .all()
    )
    agent_metadata = {
        agent_id: (name, version) for agent_id, name, version in agent_rows
    }
    entries = []
    for i, row in enumerate(finalized_rows, start=1):
        settled, rolling, rolling_count = rollout_states.get(
            row.agent_id, (None, None, None)
        )
        bonus_row = (
            efficiency_view.bonuses.get(row.agent_id)
            if efficiency_view is not None
            else None
        )
        # Read from the fleet-gated map, never directly from the persisted row:
        # the latter exists before protocol activation and is also visible on a
        # pinned v9 board.
        bounded_factor = displayed_efficiency_factors.get(row.agent_id)
        legacy_bonus = (
            bonus_row.bonus
            if bonus_row is not None
            and bounded_factor is None
            and efficiency_view is not None
            and efficiency_view.snapshot is not None
            and efficiency_view.snapshot.curve_version < 3
            else None
        )
        adjustment_present = bounded_factor is not None or legacy_bonus is not None
        entries.append(
            _public_entry(
                i,
                row,
                *agent_metadata[row.agent_id],
                finalized=True,
                score_count=score_counts.get(row.agent_id, SCORING_QUORUM),
                settled_composite=settled,
                rollout_composite=rolling,
                rollout_score_count=rolling_count,
                efficiency_bonus=legacy_bonus,
                efficiency_factor=bounded_factor,
                efficiency_fold_applied=(
                    row.agent_id in efficiency_bonuses
                    or row.agent_id in efficiency_factors
                ),
                efficiency_snapshot_id=(
                    bonus_row.snapshot_id
                    if adjustment_present and bonus_row is not None
                    else None
                ),
                efficiency_bonus_preview=(
                    (efficiency_view.preview_bonuses or {}).get(row.agent_id)
                    if efficiency_view is not None and efficiency_view.preview
                    else None
                ),
                efficiency_factor_preview=(
                    (efficiency_view.preview_factors or {}).get(row.agent_id)
                    if efficiency_view is not None and efficiency_view.preview
                    else None
                ),
                registered=(
                    row.miner_hotkey in registered_uids
                    if registered_uids is not None
                    else None
                ),
                miner_uid=(
                    registered_uids.get(row.miner_hotkey)
                    if registered_uids is not None
                    else None
                ),
                fold_stderr=fold_stderrs.get(row.agent_id),
                submission_family=_public_leaderboard_family(
                    row.family_members,
                    representative_agent_id=row.agent_id,
                ),
                official_composite=board_official_composites.get(
                    row.agent_id, row.composite
                ),
                pre_efficiency_composite=pre_efficiency_composites.get(
                    row.agent_id, row.composite
                ),
                completed_wave_count=completed_depth.get(row.agent_id, 0),
                initial_quorum_composites=tuple(quorum.get(row.agent_id, ())),
                completed_wave_composites=tuple(
                    value
                    for _seed, value in sorted(
                        completed_by_seed.get(row.agent_id, {}).items()
                    )
                ),
                # The raw, unfiltered append-only trail, alongside the
                # fold-eligible subset above. ``completed_wave_data`` keeps
                # only seeds shared by *every current* emission-set member, so a
                # new entrant with no retests yet empties that intersection and
                # ``completed_wave_*`` collapses to zero board-wide even though
                # nothing was deleted. Surfacing the raw depth separately is
                # what makes that distinction visible instead of looking like
                # data loss; the fold still consumes only the completed subset.
                confirmation_seed_depth=confirmation_depth.get(row.agent_id, 0),
                confirmation_seed_composites=tuple(
                    value
                    for _seed, value in sorted(
                        confirmation_by_seed.get(row.agent_id, {}).items()
                    )
                ),
                continual_aggregate_active=continual_mean_active,
                average_run_cost_microusd=run_costs.get(
                    (row.agent_id, row.bench_version), (None, 0)
                )[0],
                inference_run_count=run_costs.get(
                    (row.agent_id, row.bench_version), (None, 0)
                )[1],
                v9_confirmation=v9_confirmations.get(row.agent_id),
            )
        )
    for row, count in provisional_rows:
        settled, rolling, rolling_count = rollout_states.get(
            row.agent_id, (None, None, None)
        )
        entries.append(
            _public_entry(
                len(entries) + 1,
                row,
                *agent_metadata[row.agent_id],
                finalized=False,
                score_count=count,
                settled_composite=settled,
                rollout_composite=rolling,
                rollout_score_count=rolling_count,
                registered=(
                    row.miner_hotkey in registered_uids
                    if registered_uids is not None
                    else None
                ),
                miner_uid=(
                    registered_uids.get(row.miner_hotkey)
                    if registered_uids is not None
                    else None
                ),
                fold_stderr=fold_stderrs.get(row.agent_id),
                average_run_cost_microusd=run_costs.get(
                    (row.agent_id, row.bench_version), (None, 0)
                )[0],
                inference_run_count=run_costs.get(
                    (row.agent_id, row.bench_version), (None, 0)
                )[1],
                v9_confirmation=v9_confirmations.get(row.agent_id),
            )
        )
    return PublicLeaderboardResponse(
        generated_at=now,
        count=len(entries),
        current_bench_version=display_version,
        active_bench_version=active_version,
        desired_bench_version=desired_version,
        available_bench_versions=await list_scored_bench_versions(session),
        selection_mode="historical" if bench_version is not None else "authoritative",
        v9_confirmation_mode=v9_confirmation_mode,
        continual_aggregate_active=continual_mean_active,
        continual_aggregate_required_protocol=_CONTINUAL_MEAN_PROTOCOL,
        registration_stale=registration_stale,
        entries=entries,
        emissions=(
            None
            if bench_version is not None
            else _public_koth_emissions(
                emission_rows,
                stderrs=fold_stderrs,
                quorum_by_agent=quorum,
                confirmation_by_seed=(
                    confirmation_by_seed if continual_mean_active else {}
                ),
                confirmation_depth=(
                    confirmation_depth if continual_mean_active else {}
                ),
                include_continual_scores=continual_mean_active,
                wave_membership=continual_settings.wave_membership,
                anchor_version=active_version,
                efficiency_bonuses=efficiency_bonuses,
                efficiency_factors=efficiency_factors,
                tie_weighting_active=tie_weighting_active,
            )
        ),
        efficiency=_efficiency_status(efficiency_view),
    )


def _efficiency_status(
    view: EfficiencyBoardView | None,
) -> PublicEfficiencyStatus | None:
    """The board-level bonus status, whether or not the bonus is switched on.

    The block used to vanish entirely when the bonus was disabled, so an
    operator could not see the boost without turning it on -- and turning it on
    WRITES. A preview renders the same shape with ``active=False,
    preview=True``, so the dashboard can show "would be +X%" beside an explicit
    not-applied badge.
    """
    if view is None:
        return None
    if view.preview:
        reference = view.preview_reference
        if reference is None:
            return None
        deep_frontier = (
            reference.deep_frontier_ratio * reference.reference_p25_tokens
            if reference.deep_frontier_ratio is not None
            and reference.reference_p25_tokens is not None
            else None
        )
        return PublicEfficiencyStatus(
            # Never active: a preview is not applied to any composite and never
            # reaches the fold. ``preview`` is what tells the UI the numbers are
            # real arithmetic rather than a placeholder.
            active=False,
            preview=True,
            bench_version=reference.bench_version,
            run_size=reference.run_size,
            epoch_index=reference.epoch_index,
            snapshot_id=None,
            cohort_size=len(reference.members),
            candidate_count=view.preview_candidate_count,
            cost_evidence_count=view.preview_cost_evidence_count,
            quality_qualified_count=view.preview_quality_qualified_count,
            owner_deduped_count=view.preview_owner_deduped_count,
            lineage_deduped_count=view.preview_lineage_deduped_count,
            n_min=reference.n_min,
            bonus_cap=reference.bonus_cap,
            curve_version=reference.curve_version,
            deep_bonus_cap=reference.deep_bonus_cap,
            deep_frontier_tokens=deep_frontier,
            factor_alpha=reference.factor_alpha,
            minimum_factor=reference.minimum_factor,
            maximum_factor=reference.maximum_factor,
            reference_p25_tokens=reference.reference_p25_tokens,
            reference_median_tokens=reference.reference_median_tokens,
        )
    if view.snapshot is None:
        return None
    snapshot = view.snapshot
    deep_frontier_tokens = (
        snapshot.deep_frontier_ratio * snapshot.reference_p25_tokens
        if snapshot.deep_frontier_ratio is not None
        and snapshot.reference_p25_tokens is not None
        else None
    )
    return PublicEfficiencyStatus(
        active=snapshot.active,
        preview=False,
        bench_version=snapshot.bench_version,
        run_size=snapshot.run_size,
        epoch_index=snapshot.epoch_index,
        snapshot_id=snapshot.snapshot_id,
        cohort_size=len(snapshot.members or []),
        n_min=snapshot.n_min,
        bonus_cap=snapshot.bonus_cap,
        curve_version=snapshot.curve_version,
        deep_bonus_cap=snapshot.deep_bonus_cap,
        deep_frontier_tokens=deep_frontier_tokens,
        factor_alpha=snapshot.factor_alpha,
        minimum_factor=snapshot.minimum_factor,
        maximum_factor=snapshot.maximum_factor,
        reference_p25_tokens=snapshot.reference_p25_tokens,
        reference_median_tokens=snapshot.reference_median_tokens,
    )


@router.get(
    "/efficiency/snapshots/{snapshot_id}",
    response_model=PublicEfficiencySnapshotResponse,
    responses={404: {"description": "Unknown snapshot id."}},
)
async def efficiency_snapshot(
    snapshot_id: UUID,
    session: SessionDep,
    response: Response,
) -> PublicEfficiencySnapshotResponse:
    """One immutable frozen efficiency-cohort snapshot, for bonus provenance.

    Everything a third party needs to reproduce a published bonus from stored
    data: the frozen membership (lineage-deduped, exposed as opaque lineage
    group ordinals — never the raw digests), the quality floors in force, and
    the robust reference statistics (P25 frontier / median zero point).
    Snapshots never change once written, so this response is immutable.
    """
    from ditto.db.queries.efficiency import get_snapshot_by_id

    snapshot = await get_snapshot_by_id(session, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="efficiency snapshot not found")
    response.headers["Cache-Control"] = _SETTLED_BENCH_CACHE_CONTROL
    members: list[PublicEfficiencyCohortMember] = []
    for ordinal, raw in enumerate(snapshot.members or [], start=1):
        if not isinstance(raw, dict):
            continue
        members.append(
            PublicEfficiencyCohortMember(
                agent_id=UUID(str(raw["agent_id"])),
                miner_hotkey=str(raw["miner_hotkey"]),
                composite=float(raw["composite"]),
                memory_mean=float(raw["memory_mean"]),
                token_total=float(raw["token_total"]),
                lineage_group=ordinal,
                collapsed_agent_ids=[
                    UUID(str(value)) for value in raw.get("collapsed_agent_ids", [])
                ],
            )
        )
    return PublicEfficiencySnapshotResponse(
        snapshot_id=snapshot.snapshot_id,
        bench_version=snapshot.bench_version,
        run_size=snapshot.run_size,
        epoch_index=snapshot.epoch_index,
        active=snapshot.active,
        cohort_limit=snapshot.cohort_limit,
        n_min=snapshot.n_min,
        bonus_cap=snapshot.bonus_cap,
        curve_version=snapshot.curve_version,
        deep_bonus_cap=snapshot.deep_bonus_cap,
        deep_frontier_ratio=snapshot.deep_frontier_ratio,
        factor_alpha=snapshot.factor_alpha,
        minimum_factor=snapshot.minimum_factor,
        maximum_factor=snapshot.maximum_factor,
        quality_floor=snapshot.quality_floor,
        memory_floor=snapshot.memory_floor,
        reference_p25_tokens=snapshot.reference_p25_tokens,
        reference_median_tokens=snapshot.reference_median_tokens,
        computed_at=snapshot.computed_at,
        members=members,
    )


def _retained_registration(
    cached: _RegistrationSnapshot | None, *, now: float
) -> _RegistrationSnapshot | None:
    """Re-arm a previously good mapping as explicitly stale, or ``None``.

    Returns ``None`` when there has never been a good read, or when the last one
    is older than :data:`_REGISTRATION_MAX_STALE_SECONDS` and so no longer worth
    presenting as current registration.
    """
    if cached is None or cached.uids_by_hotkey is None:
        return None
    if now - cached.read_at > _REGISTRATION_MAX_STALE_SECONDS:
        return None
    return _RegistrationSnapshot(
        expires_at=now + _REGISTRATION_FAILURE_CACHE_TTL_SECONDS,
        uids_by_hotkey=cached.uids_by_hotkey,
        read_at=cached.read_at,
        stale=True,
    )


async def _current_registration(request: Request) -> _RegistrationSnapshot | None:
    """Current subnet hotkeys and UIDs, degrading to the last good read.

    Registration decorates the durable score ledger; it never deletes or changes
    a submission. A momentary chain hiccup therefore must not flip every row on
    the board to "registration unknown" for one poll and back on the next: that
    reads to a viewer as the page blanking. On a failed refresh this keeps
    serving the previous mapping with ``stale=True`` so the caller can label it,
    and only reports genuinely unknown registration (``uids_by_hotkey=None``)
    when there is no recent good read to fall back on.
    """
    chain = getattr(request.app.state, "chain", None)
    config = getattr(request.app.state, "config", None)
    if chain is None or config is None:
        return None
    now = time.monotonic()
    cached = getattr(request.app.state, "public_registration_snapshot", None)
    if not isinstance(cached, _RegistrationSnapshot):
        cached = None
    if cached is not None and cached.expires_at > now:
        return cached
    try:
        async with asyncio.timeout(_REGISTRATION_LOOKUP_TIMEOUT_SECONDS):
            neurons = await chain.get_recent_neurons(config.chain.netuid)
    except (ChainError, TimeoutError) as e:
        retained = _retained_registration(cached, now=now)
        logger.warning(
            "public leaderboard registration read failed after %.1fs (%s): %s",
            _REGISTRATION_LOOKUP_TIMEOUT_SECONDS,
            (
                f"serving last known good, {now - retained.read_at:.0f}s old"
                if retained is not None
                else "no recent read to fall back on; reporting unknown"
            ),
            _error_detail(e),
        )
        snapshot = retained or _RegistrationSnapshot(
            expires_at=now + _REGISTRATION_FAILURE_CACHE_TTL_SECONDS,
            uids_by_hotkey=None,
            read_at=now,
        )
        request.app.state.public_registration_snapshot = snapshot
        return snapshot
    snapshot = _RegistrationSnapshot(
        expires_at=now + _REGISTRATION_CACHE_TTL_SECONDS,
        uids_by_hotkey={neuron.hotkey: int(neuron.uid) for neuron in neurons},
        read_at=now,
    )
    request.app.state.public_registration_snapshot = snapshot
    return snapshot


@router.get("/health", response_model=PublicHealthResponse)
async def health(
    response: Response,
    session: SessionDep,
) -> PublicHealthResponse:
    """Aggregate subnet-health rollup (submissions + reported scores).

    Aggregate-only, like the leaderboard: miner/agent counts, last-scored time,
    24h scoring throughput, and average latency. Failure/latency-of-weights
    telemetry lives in wandb: the platform only sees successful scores.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    now = datetime.now(UTC)
    roll = await get_public_health(session, now=now)
    return PublicHealthResponse(
        generated_at=now,
        miners=roll.miners,
        scored_miners=roll.scored_miners,
        scored_agents=roll.scored_agents,
        last_scored_at=roll.last_scored_at,
        total_scores=roll.total_scores,
        scores_24h=roll.scores_24h,
        avg_latency_ms=roll.avg_latency_ms,
    )


def _leased_slots(
    assignments: list[ActiveValidatorAssignment],
) -> set[tuple[str, str]]:
    """``(validator_hotkey, slot_id)`` pairs holding a live lease right now."""
    return {
        (assignment.ticket.validator_hotkey, assignment.ticket.slot_id)
        for assignment in assignments
    }


def _public_orphaned_slot(orphan: OrphanedLease) -> PublicOrphanedSlot:
    """One evicted-but-possibly-still-executing slot, as the fleet view sees it."""
    return PublicOrphanedSlot(
        slot_id=orphan.slot_id,
        agent_id=orphan.agent_id,
        agent_name=orphan.agent_name,
        bench_version=orphan.bench_version,
        # ``released`` is filtered out before this is reached: a released slot is
        # genuinely free and must keep rendering as idle.
        state=cast(Literal["still_running", "indeterminate"], orphan.state),
        reason=orphan.reason,
        evicted_at=orphan.evicted_at,
        orphaned_for_seconds=orphan.orphaned_for_seconds,
        original_deadline=orphan.original_deadline,
        protocol_version=orphan.protocol_version,
    )


def _validator_heartbeats_response(
    *,
    rows: list[Any],
    assignments: list[ActiveValidatorAssignment],
    active_work: list[ActiveValidatorWork],
    confirmation_work: list[ActiveConfirmationWork],
    orphaned_leases: list[OrphanedLease],
    now: datetime,
    active_bench_version: int,
    slot_settings: ValidatorSlotSettings,
) -> PublicValidatorHeartbeatsResponse:
    """Reconcile platform leases and signed heartbeat claims without conflating them.

    ``confirmation_work`` is deliberately independent of ordinary active work:
    its ``longmem-*`` tickets use a separate capacity lane and must never alter
    the ordinary slot accounting or health classification rendered here.

    ``orphaned_leases`` is another independent input, and it is not derivable from the
    other two: a lease an operator evicted is gone from ``assignments`` by
    construction, and its slot is filtered out of the stored capacity that backs
    ``active_work`` for the same reason -- yet the validator may still be running
    the container. Without it every such slot reconciles cleanly to "idle", which
    is exactly the false headroom this argument exists to stop reporting.
    Required rather than defaulted so no future call site can silently reproduce
    it.

    ``slot_settings`` is required rather than defaulted: this view's whole job is
    to describe the fleet as dispatch sees it, and a caller that forgets the
    policy would silently publish advertised capacity as available capacity --
    the exact conflation the per-entry ``allowed_slots`` exists to end.
    """
    orphans_by_hotkey: dict[str, list[PublicOrphanedSlot]] = {}
    for orphan in orphaned_leases:
        if orphan.state == "released":
            continue
        orphans_by_hotkey.setdefault(orphan.validator_hotkey, []).append(
            _public_orphaned_slot(orphan)
        )
    assignments_by_hotkey: dict[str, list[ActiveValidatorAssignment]] = {}
    for assignment_row in assignments:
        assignments_by_hotkey.setdefault(
            assignment_row.ticket.validator_hotkey, []
        ).append(assignment_row)
    active_by_hotkey: dict[str, list[ActiveValidatorWork]] = {}
    for work in active_work:
        active_by_hotkey.setdefault(work.heartbeat.validator_hotkey, []).append(work)
    confirmation_by_hotkey: dict[str, list[ActiveConfirmationWork]] = {}
    for confirmation in confirmation_work:
        confirmation_by_hotkey.setdefault(
            confirmation.ticket.validator_hotkey, []
        ).append(confirmation)
    entries = []
    for row in rows:
        seen_at = cast(datetime, _aware(row.seen_at))
        metrics = _public_system_metrics(row.system_metrics)
        online, availability, health = _fleet_classification(
            state=row.state, seen_at=seen_at, now=now, metrics=metrics
        )
        issuance_paused = validator_issuance_paused(
            slot_settings, validator_hotkey=row.validator_hotkey
        )
        validator_assignments = assignments_by_hotkey.get(row.validator_hotkey, [])
        synchronized_works = active_by_hotkey.get(row.validator_hotkey, [])
        capacity = None
        if row.protocol_version >= 10:
            with contextlib.suppress(ValidationError):
                capacity = BenchmarkCapacity.model_validate(row.benchmark_capacity)
        if capacity is not None:
            by_identity = {
                (item.ticket.slot_id, item.agent.agent_id): item
                for item in validator_assignments
            }
            synchronized_works = []
            for slot in capacity.active:
                # Identity only. Every slot in the stored capacity was already
                # confirmed against a live ticket under a row lock at ingest, so
                # re-testing the deadline here — against a separately fetched
                # assignments snapshot — adds no safety and one failure mode:
                # a lease re-issued in place moves the deadline the validator
                # cached, and a single microsecond of drift blanked the slot to
                # "Benchmark progress not reported" while the run was healthy.
                item = by_identity.get((slot.slot_id, slot.agent_id))
                if item is None:
                    continue
                synchronized_works.append(
                    ActiveValidatorWork(
                        heartbeat=row,
                        ticket=item.ticket,
                        agent=item.agent,
                        progress=slot.progress,
                    )
                )
        assignment = validator_assignments[0] if validator_assignments else None
        synchronized_work = synchronized_works[0] if synchronized_works else None
        capabilities = None
        stack = None
        if row.protocol_version >= 7:
            try:
                capabilities = ValidatorCapabilities.model_validate(row.capabilities)
                stack = ValidatorStackIdentity.model_validate(row.stack)
            except ValidationError:
                # Stored telemetry is not trusted merely because it is JSON.
                # Malformed v7 data is omitted publicly and rejected for routing.
                pass
        stack_health = None
        if row.protocol_version >= 9:
            # Same posture as v7 identity: publish only what re-validates
            # against the closed schema, never raw stored JSON.
            with contextlib.suppress(ValidationError):
                stack_health = ValidatorStackHealth.model_validate(row.stack_health)
        assignment_state: ValidatorAssignmentState
        if assignment is None:
            # No live lease. Reporting an agent with no assignment is a genuine
            # mismatch (e.g. a run outliving its reopened ticket, as with a slow
            # legacy validator); otherwise the validator is simply idle.
            assignment_state = (
                "assignment_mismatch"
                if row.active_agent_id is not None
                else "unassigned"
            )
        elif seen_at < now - _VALIDATOR_ONLINE_WINDOW:
            # A quiet validator is a liveness problem, deliberately kept distinct
            # from a job/assignment problem below.
            assignment_state = "heartbeat_stale"
        elif synchronized_work is not None:
            assignment_state = "synchronized"
        else:
            issued_at = _aware(assignment.ticket.issued_at)
            if (
                issued_at is not None
                and issued_at > seen_at - _ASSIGNMENT_HANDOFF_GRACE
            ):
                # The lease was issued within the hand-off grace of this heartbeat,
                # so the validator has not yet had a chance to report picking it
                # up. A transient hand-off: this is what stops the fleet view from
                # flapping red during normal job transitions.
                assignment_state = "assigning"
            else:
                # Fresh heartbeats, lease older than the grace (or an anomalous
                # lease with no issue time), still not on the assigned agent: a
                # real job/assignment mismatch.
                assignment_state = "assignment_mismatch"
        active_benchmark = (
            _public_benchmark_progress(synchronized_work, now)
            if synchronized_work is not None
            else None
        )
        active_benchmarks = [
            _public_benchmark_progress(work, now) for work in synchronized_works
        ]
        active_benchmarks.sort(key=lambda progress: progress.slot_id)
        active_by_slot = {work.ticket.slot_id: work for work in synchronized_works}
        assigned_benchmarks = [
            _public_benchmark_progress(
                active_by_slot.get(item.ticket.slot_id)
                or ActiveValidatorWork(
                    heartbeat=row,
                    ticket=item.ticket,
                    agent=item.agent,
                    progress=None,
                ),
                now,
            )
            for item in validator_assignments
        ]
        assigned_benchmarks.sort(key=lambda progress: progress.slot_id)
        scorer_liveness, scorer_reasons = _scorer_liveness(
            capabilities, row.protocol_version
        )
        # The platform's own leasing gate, asked one question earlier: can this
        # stack serve what the fleet is scoring? A validator that cannot is
        # issued no work at all, so publishing it as healthy-and-idle described a
        # host, not a participant. The legacy era is exempt for the same reason
        # ticket issuance exempts it — below the legacy floor the platform
        # requires no capability advertisement, so nobody is gated out.
        bench_serviceability = _bench_serviceability(
            row, active_bench_version=active_bench_version
        )
        entries.append(
            PublicValidatorHeartbeat(
                validator_hotkey=row.validator_hotkey,
                software_version=row.software_version,
                protocol_version=row.protocol_version,
                state=cast(ValidatorRuntimeState, row.state),
                assigned_agent_id=(
                    assignment.agent.agent_id if assignment is not None else None
                ),
                assigned_agent_name=(
                    assignment.agent.name if assignment is not None else None
                ),
                reported_agent_id=row.active_agent_id,
                assignment_state=assignment_state,
                active_agent_id=(
                    synchronized_work.agent.agent_id
                    if synchronized_work is not None
                    else None
                ),
                active_benchmark=active_benchmark,
                configured_slots=(capacity.configured_slots if capacity else 1),
                # Keep the operator brake separate from heartbeat availability:
                # an offline paused validator remains honestly offline while
                # the dashboard can still show why it receives no new work.
                issuance_paused=issuance_paused,
                # Resolved with the very function ticket issue calls, so the
                # fleet view cannot drift from dispatch as the policy changes.
                allowed_slots=(
                    0
                    if issuance_paused
                    else allowed_slot_count(
                        slot_settings,
                        advertised_slots=(capacity.configured_slots if capacity else 1),
                        sample=HostResourceSample(
                            cpu_percent=(
                                metrics.cpu_percent if metrics is not None else None
                            ),
                            memory_percent=(
                                metrics.memory_percent if metrics is not None else None
                            ),
                            disk_percent=(
                                metrics.disk_percent if metrics is not None else None
                            ),
                        ),
                    )
                ),
                healthy_slots=(capacity.healthy_slots if capacity else ["slot-0"]),
                admission=(capacity.admission if capacity else "accepting"),
                active_benchmarks=active_benchmarks,
                assigned_benchmarks=assigned_benchmarks,
                confirmation_benchmarks=[
                    PublicConfirmationProgress(
                        bundle_id=work.bundle.bundle_id,
                        slot_id=work.ticket.slot_id,
                        mode=cast(Literal["shadow", "enforce"], work.mode.value),
                        profile_revision=work.bundle.profile_revision,
                        attempt=work.ticket.attempt,
                        issued_at=work.ticket.issued_at,
                        deadline=work.ticket.deadline,
                        subjects=[
                            PublicConfirmationSubject(
                                agent_id=subject.agent_id,
                                agent_name=subject.agent_name,
                            )
                            for subject in work.subjects
                        ],
                    )
                    for work in confirmation_by_hotkey.get(row.validator_hotkey, [])
                ],
                orphaned_slots=orphans_by_hotkey.get(row.validator_hotkey, []),
                first_seen_at=_aware(row.first_seen_at),
                reported_at=cast(datetime, _aware(row.reported_at)),
                seen_at=seen_at,
                online=online,
                availability=availability,
                # A scorer that cannot serve the benchmark being scored, or that
                # is not serving at all, outranks everything else here: the
                # validator cannot complete a single lease, which is worse than
                # any host-metric warning and must not read like one. Below it, a
                # wedged benchmark or a required stack component that is
                # degraded/unreachable/identity-mismatched is a real operational
                # problem regardless of how the host metrics look (or whether they
                # were reported), so surface it as a warning in the fleet health
                # roll-up rather than only in the nested per-component map.
                health=(
                    "critical"
                    if scorer_liveness == "not_serving"
                    or bench_serviceability != "serving"
                    else (
                        "warning"
                        if scorer_reasons
                        or (active_benchmark is not None and active_benchmark.stalled)
                        or _stack_component_issues(stack_health)
                        else health
                    )
                ),
                scorer_liveness=scorer_liveness,
                # The detailed "why" behind the badge, kept as structured labels
                # (for a tooltip) so the summary stays compact without hiding info.
                health_reasons=_health_reasons(
                    state=row.state,
                    metrics=metrics,
                    active_benchmark=active_benchmark,
                    stack_health=stack_health,
                    scorer_reasons=scorer_reasons,
                    bench_reason=_bench_serviceability_reason(
                        bench_serviceability,
                        version=active_bench_version,
                        protocol_version=row.protocol_version,
                    ),
                ),
                bench_serviceability=bench_serviceability,
                system_metrics=metrics,
                capabilities=capabilities,
                stack=stack,
                stack_health=stack_health,
            )
        )
    return PublicValidatorHeartbeatsResponse(
        generated_at=now,
        online_window_seconds=int(_VALIDATOR_ONLINE_WINDOW.total_seconds()),
        stale_window_seconds=int(_VALIDATOR_STALE_WINDOW.total_seconds()),
        active_bench_version=active_bench_version,
        slot_policy=PublicValidatorSlotPolicy(
            max_concurrent_slots=slot_settings.max_concurrent_slots,
            disk_percent_ceiling=slot_settings.disk_percent_ceiling,
        ),
        reported_count=len(entries),
        online_count=sum(entry.online for entry in entries),
        validators=entries,
    )


@router.get("/validators", response_model=PublicValidatorHeartbeatsResponse)
async def validators(
    request: Request,
    response: Response,
    session: SessionDep,
) -> PublicValidatorHeartbeatsResponse:
    """Signed reports reconciled with the platform's current assignment truth."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    now = datetime.now(UTC)
    assignments = await list_active_validator_assignments(session, now=now)
    return _validator_heartbeats_response(
        rows=await list_validator_heartbeats(session),
        assignments=assignments,
        active_work=await list_active_validator_work(
            session, now=now, cutoff=now - _VALIDATOR_ONLINE_WINDOW
        ),
        confirmation_work=await list_active_confirmation_work(session, now=now),
        orphaned_leases=await list_orphaned_leases(
            session, now=now, live_slots=_leased_slots(assignments)
        ),
        now=now,
        active_bench_version=await active_bench_version(session),
        slot_settings=await resolve_slot_settings(request.app.state),
    )


@router.get("/validator-names", response_model=PublicValidatorNamesResponse)
async def validator_names(
    request: Request,
    response: Response,
    session: SessionDep,
) -> PublicValidatorNamesResponse:
    """Cached optional Taostats labels; this route never performs external I/O."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = await list_validator_heartbeats(session)
    reporter_hotkeys = {row.validator_hotkey for row in rows}
    snapshot = request.app.state.validator_names.snapshot(sorted(reporter_hotkeys))
    return PublicValidatorNamesResponse(
        generated_at=datetime.now(UTC),
        status=snapshot.status,
        refreshed_at=snapshot.refreshed_at,
        validators=[
            PublicValidatorName(
                validator_hotkey=hotkey,
                display_name=snapshot.names.get(hotkey),
                stake_weight=snapshot.stake_weights.get(hotkey),
            )
            for hotkey in sorted(snapshot.names.keys() | snapshot.stake_weights.keys())
            if hotkey in reporter_hotkeys
        ],
    )


@router.get("/screeners", response_model=PublicScreenerHeartbeatsResponse)
async def screeners(
    response: Response,
    session: SessionDep,
) -> PublicScreenerHeartbeatsResponse:
    """Authenticated screener fleet reports with a strict public allowlist."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    now = datetime.now(UTC)
    rows = await list_screener_heartbeats(session)
    enrolled_nodes = {
        row.screener_hotkey: row for row in await session.scalars(select(ScreenerNode))
    }
    active_ids = [
        row.active_agent_id
        for row in rows
        if row.state == "screening" and row.active_agent_id is not None
    ]
    attempts = await get_running_screening_attempts(session, agent_ids=active_ids)
    agents = {
        agent.agent_id: agent
        for agent in await session.scalars(
            select(Agent).where(Agent.agent_id.in_(active_ids))
        )
    }
    entries = []
    for row in rows:
        enrolled_node = enrolled_nodes.get(row.screener_hotkey)
        seen_at = cast(datetime, _aware(row.seen_at))
        metrics = _screener_system_metrics(row.system_metrics)
        online, availability, health = _fleet_classification(
            state=row.state, seen_at=seen_at, now=now, metrics=metrics
        )
        active_agent_id = row.active_agent_id
        active_agent = (
            agents.get(active_agent_id) if active_agent_id is not None else None
        )
        active_attempt = (
            attempts.get(active_agent_id) if active_agent_id is not None else None
        )
        active_work = bool(
            online
            and row.state == "screening"
            and active_agent is not None
            and active_agent.status == AgentStatus.SCREENING
            and active_attempt is not None
            and active_attempt.screener_hotkey == row.screener_hotkey
            and cast(datetime, _aware(active_attempt.deadline)) >= now
        )
        progress = (
            _stored_screener_progress(row.system_metrics) if active_work else None
        )
        public_progress = None
        if progress is not None and active_attempt is not None:
            progress_started = datetime.fromtimestamp(progress.started_at, tz=UTC)
            attempt_started = cast(datetime, _aware(active_attempt.started_at))
            if (
                attempt_started - _VALIDATOR_ONLINE_WINDOW
                <= progress_started
                <= seen_at + _VALIDATOR_ONLINE_WINDOW
            ):
                public_progress = PublicScreenerProgress(
                    stage=progress.stage,
                    started_at=progress_started,
                )
        entries.append(
            PublicScreenerHeartbeat(
                instance_id=row.instance_id,
                screener_hotkey=row.screener_hotkey,
                provider=cast(
                    Literal["gcp", "targon", "hetzner", "home", "test"],
                    enrolled_node.provider if enrolled_node is not None else "gcp",
                ),
                node_status=cast(
                    Literal["active", "draining", "quarantined", "revoked"],
                    enrolled_node.status if enrolled_node is not None else "active",
                ),
                capacity=(enrolled_node.capacity if enrolled_node is not None else 1),
                software_version=row.software_version,
                protocol_version=row.protocol_version,
                policy_version=row.policy_version,
                state=cast(ScreenerRuntimeState, row.state),
                active_agent_id=active_agent_id if active_work else None,
                active_agent_name=(
                    active_agent.name
                    if active_work and active_agent is not None
                    else None
                ),
                screening_progress=public_progress,
                first_seen_at=_aware(row.first_seen_at),
                reported_at=cast(datetime, _aware(row.reported_at)),
                seen_at=seen_at,
                online=online,
                availability=availability,
                health=health,
                system_metrics=metrics,
            )
        )
    return PublicScreenerHeartbeatsResponse(
        generated_at=now,
        online_window_seconds=int(_VALIDATOR_ONLINE_WINDOW.total_seconds()),
        stale_window_seconds=int(_VALIDATOR_STALE_WINDOW.total_seconds()),
        reported_count=len(entries),
        online_count=sum(entry.online for entry in entries),
        screeners=entries,
    )


def _median_composite(row: SubmissionRow) -> float | None:
    """Median of the reported composites: the canonical score, or None if unscored."""
    if not row.scores:
        return None
    return statistics.median(s.composite for s in row.scores)


def _ticket_deadline(score: Score) -> datetime | None:
    """Read the signed lease identity from score details; null means legacy."""
    if not isinstance(score.details, dict):
        return None
    value = score.details.get("ticket_deadline")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _score_bench_version(score: Score) -> int | None:
    """Read the persisted benchmark epoch without guessing for legacy rows."""
    if not isinstance(score.details, dict):
        return None
    value = score.details.get("bench_version")
    return value if isinstance(value, int) and value > 0 else None


def _datagen_version(bench_version: int | None) -> str | None:
    """Resolve a benchmark epoch only when its exact generator pin is known."""
    if bench_version is None:
        return None
    return _DATAGEN_VERSION_BY_BENCH_VERSION.get(bench_version)


def _dataset_command(
    *, seed: int, run_size: str | None, bench_version: int | None, sha_only: bool
) -> str | None:
    """Return the documented deterministic generator command for a score."""
    datagen_version = _datagen_version(bench_version)
    monorepo_ref = (
        _DATAGEN_MONOREPO_REF_BY_BENCH_VERSION.get(bench_version)
        if bench_version is not None
        else None
    )
    if run_size not in _DATAGEN_RUN_SIZES or (
        datagen_version is None and monorepo_ref is None
    ):
        return None
    version_flag = (
        ""
        if bench_version in _BENCH_VERSIONS_WITHOUT_VERSION_FLAG
        else f" -bench-version {bench_version}"
    )
    arguments = f"{version_flag} -seed {seed} -run-size {run_size}"
    output = " -sha" if sha_only else ' -out "$output"'
    if monorepo_ref is not None:
        if (
            re.fullmatch(r"(?:[0-9a-f]{40}|v[0-9]+\.[0-9]+\.[0-9]+)", monorepo_ref)
            is None
        ):
            return None
        return (
            'tmp="$(mktemp -d)" && output="$(pwd)/dataset.json" && '
            "trap 'rm -rf \"$tmp\"' EXIT && "
            "git clone --filter=blob:none --no-checkout "
            "https://github.com/ditto-assistant/ditto-subnet.git "
            '"$tmp/ditto-subnet" && '
            f'git -C "$tmp/ditto-subnet" fetch --depth=1 origin {monorepo_ref} && '
            'git -C "$tmp/ditto-subnet" checkout --detach FETCH_HEAD && '
            '(cd "$tmp/ditto-subnet/research/dittobench-datagen" && '
            f"go run ./cmd/generate{arguments}{output})"
        )
    command = (
        "go run github.com/ditto-assistant/dittobench-datagen/cmd/generate@"
        f"{datagen_version}{arguments}"
    )
    return f"{command} -sha" if sha_only else f"{command} -out dataset.json"


def _submission_scores(
    row: SubmissionRow,
    *,
    artifact_release: PublicArtifactRelease,
    v9_confirmation: V9ConfirmationPublicProjection | None = None,
) -> PublicSubmissionScores:
    """Map a submission row to the full public k=3 record."""
    return PublicSubmissionScores(
        agent_id=row.agent_id,
        miner_hotkey=row.miner_hotkey,
        status=row.status.value,
        artifact_release=artifact_release,
        quorum=SCORING_QUORUM,
        score_count=len(row.scores),
        median_composite=_median_composite(row),
        v9_confirmation_status=(
            v9_confirmation.result_status
            if v9_confirmation is not None
            else (
                "base_only" if any(s.bench_version == 9 for s in row.scores) else None
            )
        ),
        v9_full_confirmed_composite=(
            v9_confirmation.full_confirmed_composite
            if v9_confirmation is not None
            else None
        ),
        v9_confirmation_receipt=(
            V9ConfirmationReceipt.model_validate(v9_confirmation.receipt)
            if v9_confirmation is not None and v9_confirmation.receipt is not None
            else None
        ),
        dataset_seed=row.dataset_seed,
        dataset_sha256=row.dataset_sha256,
        dataset_run_size=row.dataset_run_size,
        dataset_seed_block=row.dataset_seed_block,
        dataset_seed_block_hash=row.dataset_seed_block_hash,
        scores=[_public_validator_score(s) for s in row.scores],
        generated_at=datetime.now(UTC),
    )


def _public_validator_score(s) -> PublicValidatorScore:
    """Map one stored score row to its published, redacted form."""
    details = s.details if isinstance(s.details, dict) else {}
    robustness, audit_pairs = _safe_transform_robustness(details)
    public_model_use = _safe_model_use(details)
    bench_version = _score_bench_version(s)
    return PublicValidatorScore(
        validator_hotkey=s.validator_hotkey,
        composite=s.composite,
        tool_mean=s.tool_mean,
        memory_mean=s.memory_mean,
        raw_composite=_safe_raw_composite(details),
        token_usage=_safe_token_usage(details),
        model_use=public_model_use,
        token_efficiency=_safe_token_efficiency(details),
        composite_breakdown=_composite_breakdown(
            tool_mean=s.tool_mean,
            memory_mean=s.memory_mean,
            final_composite=s.composite,
            details=details,
        ),
        v9_base=(_safe_public_v9_base(details) if bench_version == 9 else None),
        median_ms=s.median_ms,
        n=s.n,
        bench_version=bench_version,
        seed=s.seed,
        run_id=s.run_id,
        ticket_deadline=_ticket_deadline(s),
        signature=s.signature,
        generated_at=s.generated_at,
        case_results=_safe_case_results(details),
        transcript_sha256=_safe_transcript_sha256(details),
        transform_robustness=robustness,
        audit_case_count=audit_pairs,
    )


def _submission_summary(
    row: SubmissionRow, *, artifact_release: PublicArtifactRelease
) -> PublicSubmissionSummary:
    """Map a submission row to the compact index entry."""
    return PublicSubmissionSummary(
        agent_id=row.agent_id,
        miner_hotkey=row.miner_hotkey,
        status=row.status.value,
        artifact_release=artifact_release,
        score_count=len(row.scores),
        median_composite=_median_composite(row),
        dataset_seed=row.dataset_seed,
        dataset_sha256=row.dataset_sha256,
        last_scored_at=row.last_scored_at,
    )


def _public_activity_status(
    status: AgentStatus,
    *,
    screening_policy_version: int,
    has_active_attempt: bool,
    has_active_validation: bool,
    has_live_assignment: bool = False,
    score_count: int = 0,
    highest_composite: float | None = None,
    score_continuation_floor: float | None = None,
    benchmark_admitted: bool = True,
    retired: bool = False,
) -> str:
    """Collapse internal moderation detail into stable public lifecycle labels."""
    needs_rescreen = (
        status
        in (
            AgentStatus.EVALUATING,
            AgentStatus.REJECTED,
        )
        and screening_policy_version < SCREENING_POLICY_VERSION
    )
    if has_active_attempt or status == AgentStatus.SCREENING:
        return AgentStatus.SCREENING.value
    if status in (AgentStatus.UPLOADED, AgentStatus.SCREENING_FAILED) or needs_rescreen:
        return "waiting_screening"
    if status in (AgentStatus.SCREENING_PASSED, AgentStatus.EVALUATING):
        # Checked before ``not_queued`` because it is the more specific and more
        # useful answer. Both mean "not in the active queue", but ``not_queued``
        # reads as a state the submission could still leave, while a retirement
        # names the reason it never will: the benchmark generation it was
        # submitted against has closed. A retired row keeps this status even
        # while an already-issued ticket drains, so the label cannot flicker.
        if retired:
            return "retired"
        if (
            not benchmark_admitted
            and not has_active_validation
            and not has_live_assignment
        ):
            return "not_queued"
        if (
            status == AgentStatus.EVALUATING
            and not has_live_assignment
            and score_count == SCORING_QUORUM - 1
            and highest_composite is not None
            and score_continuation_floor is not None
            and highest_composite < score_continuation_floor
        ):
            return "below_score_floor"
        return "evaluating" if has_active_validation else "waiting_validator"
    if status in (AgentStatus.ATH_PENDING_REVIEW, AgentStatus.QUARANTINED):
        return "under_review"
    if status == AgentStatus.BANNED:
        return "rejected"
    return status.value


def _waiting_validator_agent_ids(
    rows: list[Any],
    *,
    statuses: dict[UUID, str],
) -> list[UUID]:
    """Submissions the fleet still owes a score, in the order they were listed.

    The population the queue preview ranks. ``below_score_floor`` belongs to it:
    the allocator still serves those rows, just last.
    """
    return [
        row.agent.agent_id
        for row in rows
        if statuses.get(row.agent.agent_id)
        in ("waiting_validator", "below_score_floor")
    ]


async def queue_preview_for_rows(
    session: AsyncSession,
    *,
    rows: list[Any],
    statuses: dict[UUID, str],
    bench_version: int,
    now: datetime,
    score_continuation_floor: float | None,
    provisional_contender_floor: float | None,
    rollout: BenchmarkRollout | None,
    waiting_agent_ids: list[UUID] | None = None,
) -> dict[UUID, QueuePreviewEntry]:
    """Rank the waiting population with the allocator's own ordering.

    Every endpoint that publishes ``validator_queue_rank`` goes through here,
    so a fix landed for one of them cannot miss the other -- which is exactly
    how the operations board kept ranking a stranded previous-generation
    backlog at the head of the queue after ``/activity`` had been corrected.
    """
    waiting = (
        waiting_agent_ids
        if waiting_agent_ids is not None
        else _waiting_validator_agent_ids(rows, statuses=statuses)
    )
    if not waiting:
        return {}
    return await preview_queue_order(
        session,
        bench_version=bench_version,
        now=now,
        agent_ids=waiting,
        score_continuation_floor=score_continuation_floor,
        provisional_contender_floor=provisional_contender_floor,
        rollout=rollout,
        previous_generation_agent_ids=await prev_generation_agent_ids(
            session,
            bench_version=bench_version,
            agent_ids=waiting,
            rollout=rollout,
        ),
    )


def _queue_rank(preview: dict[UUID, QueuePreviewEntry], agent_id: UUID) -> int | None:
    """This submission's place in the fleet-wide queue, if it is waiting on one."""
    entry = preview.get(agent_id)
    return None if entry is None else entry.rank


def _queue_gate(
    preview: dict[UUID, QueuePreviewEntry], agent_id: UUID
) -> QueueGate | None:
    """What holds this submission behind its rank, if anything does."""
    entry = preview.get(agent_id)
    return None if entry is None else entry.gate


def _queue_gate_detail(
    preview: dict[UUID, QueuePreviewEntry], agent_id: UUID
) -> str | None:
    """The evidence behind the gate, when the gate has any worth naming.

    Read off the same preview entry as :func:`_queue_gate` rather than
    recomputed, so the badge and its explanation cannot describe different
    rows -- which is the entire reason the gate and its reason live in one
    expression upstream.
    """
    entry = preview.get(agent_id)
    return None if entry is None else entry.gate_detail


def _public_activity_statuses(
    rows: list[Any],
    *,
    active_work: list[ActiveValidatorWork],
    active_assignment_agent_ids: set[UUID],
    score_continuation_floor: float | None,
    active_bench_version: int | None = None,
    benchmark_admitted_agent_ids: set[UUID] | None = None,
    retired_agent_ids: set[UUID] | None = None,
) -> dict[UUID, str]:
    """Public lifecycle label per submission, keyed by agent id.

    Split out so an endpoint can resolve the waiting population -- and rank it
    against the database -- before projecting the response.
    """
    active_agent_ids = {
        work.agent.agent_id
        for work in active_work
        if active_bench_version is None
        or work.ticket.bench_version == active_bench_version
    }
    admitted = benchmark_admitted_agent_ids
    retired = retired_agent_ids or set()
    return {
        row.agent.agent_id: _public_activity_status(
            row.agent.status,
            screening_policy_version=row.agent.screening_policy_version,
            has_active_attempt=row.screening_attempt is not None,
            has_active_validation=row.agent.agent_id in active_agent_ids,
            has_live_assignment=row.agent.agent_id in active_assignment_agent_ids,
            score_count=row.score_count,
            highest_composite=row.highest_composite,
            score_continuation_floor=score_continuation_floor,
            benchmark_admitted=(admitted is None or row.agent.agent_id in admitted),
            retired=row.agent.agent_id in retired,
        )
        for row in rows
    }


def _public_activity_response(
    *,
    rows: list[Any],
    active_work: list[ActiveValidatorWork],
    now: datetime,
    page: int,
    limit: int,
    requested_statuses: set[str],
    downloadable_only: bool,
    query: str | None,
    score_continuation_floor: float | None,
    active_assignment_agent_ids: set[UUID],
    artifact_releases: dict[UUID, PublicArtifactRelease],
    queue_preview: dict[UUID, QueuePreviewEntry],
    active_bench_version: int | None = None,
    benchmark_admitted_agent_ids: set[UUID] | None = None,
    retry_states: dict[UUID, AgentRetryState] | None = None,
    duplicate_metadata: dict[UUID, tuple[str, int | None]] | None = None,
    ath_reviews: dict[UUID, _PublicAthReviewSnapshot] | None = None,
    ath_review_composite: dict[UUID, float] | None = None,
    retired_agent_ids: set[UUID] | None = None,
    ath_only: bool = False,
    terminal_history_limit: int | None = None,
    precomputed_statuses: dict[UUID, str] | None = None,
    precomputed_status_counts: dict[str, int] | None = None,
    precomputed_downloadable_count: int | None = None,
    precomputed_total: int | None = None,
    already_paginated: bool = False,
    precomputed_page_size: int | None = None,
) -> PublicActivityResponse:
    """Project activity from the same validated work set used by fleet health."""
    active_by_agent: dict[UUID, list[PublicBenchmarkProgress]] = {}
    for work in active_work:
        active_by_agent.setdefault(work.agent.agent_id, []).append(
            _public_benchmark_progress(work, now)
        )
    board_active_agent_ids = {
        agent_id
        for agent_id, progress_rows in active_by_agent.items()
        if active_bench_version is None
        or any(
            progress.bench_version == active_bench_version for progress in progress_rows
        )
    }
    retry_by_agent = retry_states or {}
    statuses = (
        precomputed_statuses
        if precomputed_statuses is not None
        else _public_activity_statuses(
            rows,
            active_work=active_work,
            active_assignment_agent_ids=active_assignment_agent_ids,
            score_continuation_floor=score_continuation_floor,
            active_bench_version=active_bench_version,
            benchmark_admitted_agent_ids=benchmark_admitted_agent_ids,
            retired_agent_ids=retired_agent_ids,
        )
    )
    projected = [(row, statuses[row.agent.agent_id]) for row in rows]
    if ath_only:
        projected = [
            (row, row_status)
            for row, row_status in projected
            if row.agent.status == AgentStatus.ATH_PENDING_REVIEW
        ]
    normalized_query = query.strip().casefold() if query else ""
    if normalized_query:
        projected = [
            (row, row_status)
            for row, row_status in projected
            if normalized_query
            in " ".join(
                (
                    row.agent.name,
                    str(row.agent.agent_id),
                    row.agent.miner_hotkey,
                    row_status,
                )
            ).casefold()
        ]

    status_counts: dict[str, int] = precomputed_status_counts or {}
    if precomputed_status_counts is None:
        for _, row_status in projected:
            status_counts[row_status] = status_counts.get(row_status, 0) + 1
    downloadable_count = (
        precomputed_downloadable_count
        if precomputed_downloadable_count is not None
        else sum(
            1
            for row, _ in projected
            if artifact_releases[row.agent.agent_id].download_available
        )
    )
    if requested_statuses and not already_paginated:
        projected = [
            (row, row_status)
            for row, row_status in projected
            if row_status in requested_statuses
        ]
    if downloadable_only and not already_paginated:
        projected = [
            (row, row_status)
            for row, row_status in projected
            if artifact_releases[row.agent.agent_id].download_available
        ]

    total = precomputed_total if precomputed_total is not None else len(projected)
    if already_paginated:
        page_rows = projected
        page_size = precomputed_page_size or limit
    elif terminal_history_limit is None:
        page_rows = projected[(page - 1) * limit : page * limit]
        page_size = limit
    else:
        # The operations board needs every actionable submission, but not the
        # complete historical ledger on every eight-second poll. Keep all live
        # work and only the newest finalized rows; the paginated activity API
        # remains the authoritative route for full history and search.
        board_statuses = {
            "waiting_screening",
            "screening",
            "waiting_validator",
            "below_score_floor",
            "evaluating",
            "under_review",
        }
        page_rows = _operations_activity_rows(
            projected,
            board_statuses=board_statuses,
            board_active_agent_ids=board_active_agent_ids,
            terminal_history_limit=terminal_history_limit,
        )
        page_size = max(1, len(page_rows))
    return PublicActivityResponse(
        generated_at=now,
        count=len(page_rows),
        total=total,
        status_counts=status_counts,
        downloadable_count=downloadable_count,
        page=page,
        page_size=page_size,
        total_pages=max(1, math.ceil(total / limit)),
        entries=[
            PublicActivityEntry(
                agent_id=row.agent.agent_id,
                miner_hotkey=row.agent.miner_hotkey,
                name=row.agent.name,
                version=row.agent.version,
                status=row_status,
                artifact_release=artifact_releases[row.agent.agent_id],
                submitted_at=row.agent.created_at,
                last_scored_at=_aware(row.last_scored_at),
                screening_reason=(
                    None
                    if row_status in ("waiting_screening", "screening")
                    else row.agent.screening_reason
                ),
                duplicate_of=row.agent.duplicate_of,
                duplicate_name=(duplicate_metadata or {}).get(
                    row.agent.duplicate_of, (None, None)
                )[0],
                duplicate_version=(duplicate_metadata or {}).get(
                    row.agent.duplicate_of, (None, None)
                )[1],
                review_reason=(
                    (ath_reviews or {})[row.agent.agent_id].reason
                    if row.agent.agent_id in (ath_reviews or {})
                    else row.agent.review_reason
                ),
                review_event=(
                    (ath_reviews or {})[row.agent.agent_id].event
                    if row.agent.agent_id in (ath_reviews or {})
                    else None
                ),
                review_event_at=(
                    (ath_reviews or {})[row.agent.agent_id].event_at
                    if row.agent.agent_id in (ath_reviews or {})
                    else None
                ),
                review_original_reason=(
                    (ath_reviews or {})[row.agent.agent_id].original_reason
                    if row.agent.agent_id in (ath_reviews or {})
                    else None
                ),
                review_opened_at=(
                    (ath_reviews or {})[row.agent.agent_id].opened_at
                    if row.agent.agent_id in (ath_reviews or {})
                    else None
                ),
                preserved_composite=(ath_review_composite or {}).get(
                    row.agent.agent_id
                ),
                score_count=row.score_count,
                provisional_composite=row.provisional_composite,
                validator_queue_rank=_queue_rank(queue_preview, row.agent.agent_id),
                validator_queue_gate=_queue_gate(queue_preview, row.agent.agent_id),
                validator_queue_gate_detail=_queue_gate_detail(
                    queue_preview, row.agent.agent_id
                ),
                # #458's flag, now read off the shared preview rather than
                # recomputed here: it is exactly the ``previous_generation``
                # case of ``validator_queue_gate``. Kept as its own boolean
                # because clients already consume it, and derived from the one
                # gate so the two can never disagree about the same row. Both
                # are scoped to the waiting lanes for the same reason as
                # ``retry_state``: on a finalized row "predates the era" is
                # inert history, while on a waiting row it is the reason the
                # row is not moving.
                previous_generation=(
                    _queue_gate(queue_preview, row.agent.agent_id)
                    == "previous_generation"
                ),
                quorum=SCORING_QUORUM,
                retry_state=(
                    retry_by_agent[row.agent.agent_id].state
                    if row_status in ("waiting_validator", "below_score_floor")
                    and row.agent.agent_id in retry_by_agent
                    else None
                ),
                retry_after=(
                    retry_by_agent[row.agent.agent_id].earliest_retry_after
                    if row_status in ("waiting_validator", "below_score_floor")
                    and row.agent.agent_id in retry_by_agent
                    else None
                ),
                screening_policy_version=row.agent.screening_policy_version,
                required_screening_policy_version=SCREENING_POLICY_VERSION,
                screening_attempt_id=(
                    row.screening_attempt.attempt_id
                    if row.screening_attempt is not None
                    else None
                ),
                screening_build_only=(
                    row.screening_attempt.build_only
                    if row.screening_attempt is not None
                    else None
                ),
                screening_started_at=(
                    row.screening_attempt.started_at
                    if row.screening_attempt is not None
                    else None
                ),
                screening_deadline=(
                    row.screening_attempt.deadline
                    if row.screening_attempt is not None
                    else None
                ),
                active_benchmarks=active_by_agent.get(row.agent.agent_id, []),
            )
            for row, row_status in page_rows
        ],
    )


def _operations_activity_rows(
    projected: list[tuple[Any, str]],
    *,
    board_statuses: set[str],
    board_active_agent_ids: set[UUID],
    terminal_history_limit: int,
) -> list[tuple[Any, str]]:
    """Keep every live board row plus a bounded finalized history."""
    actionable = [
        item
        for item in projected
        if item[1] in board_statuses or item[0].agent.agent_id in board_active_agent_ids
    ]
    terminal = [
        item
        for item in projected
        if item[1] in {"scored", "live"}
        and item[0].agent.agent_id not in board_active_agent_ids
    ][:terminal_history_limit]
    return actionable + terminal


async def _duplicate_submission_metadata(
    session: AsyncSession, rows: list[Any]
) -> dict[UUID, tuple[str, int | None]]:
    """Resolve safe display metadata for copy-review comparison targets."""
    duplicate_ids = {
        row.agent.duplicate_of for row in rows if row.agent.duplicate_of is not None
    }
    if not duplicate_ids:
        return {}
    return {
        agent_id: (name, version)
        for agent_id, name, version in (
            await session.execute(
                select(Agent.agent_id, Agent.name, Agent.version).where(
                    Agent.agent_id.in_(duplicate_ids)
                )
            )
        )
        .tuples()
        .all()
    }


@dataclass(frozen=True, slots=True)
class _PublicAthReviewSnapshot:
    """Public-safe projection of the latest durable ATH lifecycle event."""

    event: Literal["opened", "reopened", "cleared", "rejected"]
    reason: str
    event_at: datetime
    opened_at: datetime
    original_reason: str


async def _ath_review_public_snapshot(
    session: AsyncSession, rows: list[Any]
) -> tuple[dict[UUID, _PublicAthReviewSnapshot], dict[UUID, float]]:
    """Load current ATH reasons plus canonical composites for active holds.

    ``Agent.review_reason`` intentionally preserves the first hold reason for
    lifecycle guards. Public projection must instead follow the durable action
    ledger: a reopened hold is explained by its newest ``reopen`` action and a
    resolved review by its clear/reject resolution. The original reason remains
    available only as explicitly labelled history.
    """
    agent_ids = {row.agent.agent_id for row in rows}
    if not agent_ids:
        return {}, {}
    reviews = list(
        await session.scalars(
            select(AthReview).where(AthReview.agent_id.in_(agent_ids))
        )
    )
    latest_actions: dict[UUID, AthReviewAction] = {}
    if reviews:
        actions = await session.scalars(
            select(AthReviewAction)
            .where(
                AthReviewAction.review_id.in_([review.review_id for review in reviews])
            )
            .order_by(
                AthReviewAction.review_id,
                AthReviewAction.created_at.desc(),
                AthReviewAction.action_id.desc(),
            )
        )
        for action in actions:
            latest_actions.setdefault(action.review_id, action)

    snapshots: dict[UUID, _PublicAthReviewSnapshot] = {}
    for review in reviews:
        latest = latest_actions.get(review.review_id)
        if review.status == "pending":
            if latest is not None and latest.action == "reopen":
                event: Literal["opened", "reopened", "cleared", "rejected"] = "reopened"
                reason = latest.reason
                event_at = latest.created_at
            else:
                event = "opened"
                reason = review.original_reason or "Submission routed to ATH review."
                event_at = review.opened_at
            opened_at = review.reopened_at or review.opened_at
        else:
            resolution = review.resolution or (latest.action if latest else None)
            event = "rejected" if resolution == "reject" else "cleared"
            reason = (
                review.resolution_reason
                or (latest.reason if latest is not None else None)
                or "ATH review resolved."
            )
            event_at = review.resolved_at or (
                latest.created_at if latest is not None else review.opened_at
            )
            opened_at = review.reopened_at or review.opened_at
        snapshots[review.agent_id] = _PublicAthReviewSnapshot(
            event=event,
            reason=reason,
            event_at=event_at,
            opened_at=opened_at,
            original_reason=review.original_reason
            or "Submission routed to ATH review.",
        )

    active_agent_ids = {
        review.agent_id for review in reviews if review.status == "pending"
    }
    if not active_agent_ids:
        return snapshots, {}
    composites: dict[UUID, list[float]] = {}
    for agent_id, composite in (
        await session.execute(
            select(Score.agent_id, Score.composite).where(
                Score.agent_id.in_(active_agent_ids)
            )
        )
    ).tuples():
        composites.setdefault(agent_id, []).append(float(composite))
    # Match the canonical median used when the score quorum finalized.
    medians = {
        agent_id: float(statistics.median(values))
        for agent_id, values in composites.items()
    }
    return snapshots, medians


@router.get("/activity", response_model=PublicActivityResponse)
async def activity(
    request: Request,
    response: Response,
    session: SessionDep,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    status: Annotated[list[str] | None, Query()] = None,
    downloadable: bool = Query(default=False),
    review: Literal["ath"] | None = Query(default=None),
    q: str | None = Query(default=None, min_length=1, max_length=200),
) -> PublicActivityResponse:
    """Recent submissions and their safe public pipeline stage, newest first.

    This exposes the evidence a miner needs to understand a failure or review:
    a safe screening category plus the duplicate reference and anti-copy signal
    summary. Artifact locations, hashes, payments, and raw build logs remain
    private.
    """
    response.headers["Cache-Control"] = "public, max-age=10"
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    now = datetime.now(UTC)
    if efficiency_config.enabled:
        await ensure_efficiency_state(session, efficiency_config, now=now)
    requested_statuses = set(status or [])
    unknown_statuses = requested_statuses - _PUBLIC_ACTIVITY_STATUSES
    if unknown_statuses:
        raise HTTPException(
            status_code=422,
            detail="unknown public activity status: "
            + ", ".join(sorted(unknown_statuses)),
        )

    active_version = await active_bench_version(session)
    assignments = await list_active_validator_assignments(session, now=now)
    active_work = await list_active_validator_work(
        session,
        now=now,
        cutoff=now - _VALIDATOR_ONLINE_WINDOW,
        assignments=assignments,
    )
    (
        score_continuation_floor,
        provisional_contender_floor,
    ) = await get_score_priority_floors(
        session,
        bench_version=active_version,
        efficiency_config=efficiency_config,
        now=now,
        active_version=active_version,
    )
    active_assignment_agent_ids = {
        assignment.agent.agent_id
        for assignment in assignments
        if assignment.ticket.bench_version == active_version
    }
    active_validation_agent_ids = {
        work.agent.agent_id
        for work in active_work
        if work.ticket.bench_version == active_version
    }
    release_policy = await artifact_release_policy(session)
    downloadable_agent_ids = await available_public_source_agent_ids(
        session,
        quorum=SCORING_QUORUM,
        policy=release_policy,
        now=now,
    )
    activity_page = await query_public_activity_page(
        session,
        bench_version=active_version,
        page=page,
        limit=limit,
        requested_statuses=requested_statuses,
        downloadable_only=downloadable,
        downloadable_agent_ids=downloadable_agent_ids,
        query=q,
        ath_only=review == "ath",
        active_validation_agent_ids=active_validation_agent_ids,
        active_assignment_agent_ids=active_assignment_agent_ids,
        score_continuation_floor=score_continuation_floor,
    )
    rows = activity_page.rows
    statuses = {row.agent.agent_id: cast(str, row.public_status) for row in rows}
    artifact_releases = await _artifact_release_snapshot(
        session,
        statuses={row.agent.agent_id: row.agent.status for row in rows},
        now=now,
        policy=release_policy,
    )
    ath_reviews, ath_composite = await _ath_review_public_snapshot(session, rows)
    queue_preview = await queue_preview_for_rows(
        session,
        rows=rows,
        statuses=statuses,
        bench_version=active_version,
        now=now,
        score_continuation_floor=score_continuation_floor,
        provisional_contender_floor=provisional_contender_floor,
        rollout=activity_page.activated_rollout,
        waiting_agent_ids=activity_page.waiting_agent_ids,
    )
    return _public_activity_response(
        rows=rows,
        active_work=active_work,
        now=now,
        page=page,
        limit=limit,
        requested_statuses=requested_statuses,
        downloadable_only=downloadable,
        query=q,
        score_continuation_floor=score_continuation_floor,
        active_assignment_agent_ids=active_assignment_agent_ids,
        artifact_releases=artifact_releases,
        queue_preview=queue_preview,
        active_bench_version=active_version,
        retry_states=await classify_agent_retry_states(
            session,
            agents=[row.agent for row in rows],
            now=now,
            canonical_version=active_version,
        ),
        duplicate_metadata=await _duplicate_submission_metadata(session, rows),
        ath_reviews=ath_reviews,
        ath_review_composite=ath_composite,
        precomputed_statuses=statuses,
        precomputed_status_counts=activity_page.status_counts,
        precomputed_downloadable_count=activity_page.downloadable_count,
        precomputed_total=activity_page.total,
        already_paginated=True,
        precomputed_page_size=limit,
    )


@router.get(
    "/screener-capacity-watchdog",
    response_model=PublicScreenerWatchdogResponse,
)
async def screener_capacity_watchdog(
    response: Response,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> PublicScreenerWatchdogResponse:
    """Tell the GCP-only watchdog whether the normal writer lease is stale."""
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(UTC)
    snapshot = await session.get(ScreenerCapacitySnapshot, environment)
    if snapshot is None:
        return PublicScreenerWatchdogResponse(
            generated_at=now,
            controller_stale=True,
            activate_fallback=True,
            reason="controller_missing",
            controller_epoch=None,
            controller_source_sha=None,
            provider_ready=False,
        )
    expiry = snapshot.controller_lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    stale = now >= expiry
    activate_fallback = stale or not snapshot.provider_ready
    reason: Literal["controller_fresh", "controller_stale", "provider_not_ready"]
    if stale:
        reason = "controller_stale"
    elif not snapshot.provider_ready:
        reason = "provider_not_ready"
    else:
        reason = "controller_fresh"
    return PublicScreenerWatchdogResponse(
        generated_at=now,
        controller_stale=stale,
        activate_fallback=activate_fallback,
        reason=reason,
        controller_epoch=snapshot.controller_epoch,
        controller_source_sha=snapshot.controller_source_sha,
        provider_ready=snapshot.provider_ready and not stale,
    )


async def _public_submission_build_snapshot(
    session: AsyncSession,
    *,
    now: datetime,
    environment: str = "prod",
) -> PublicSubmissionImageBuildSnapshot:
    """Project recent build provenance without provider or job credentials."""
    cutoff = now - _SUBMISSION_BUILD_WINDOW
    active_statuses = ("queued", "leased", "running")
    completed_statuses = ("succeeded", "consumed")
    active_count, targon_completed_count, fallback_authorized_count = (
        await session.execute(
            select(
                func.count().filter(SubmissionImageBuild.status.in_(active_statuses)),
                func.count().filter(
                    SubmissionImageBuild.provider == "targon",
                    SubmissionImageBuild.status.in_(completed_statuses),
                ),
                func.count().filter(SubmissionImageBuild.status == "fallback_required"),
            ).where(
                SubmissionImageBuild.environment == environment,
                SubmissionImageBuild.updated_at >= cutoff,
            )
        )
    ).one()
    recent = (
        await session.execute(
            select(SubmissionImageBuild, Agent.name, Agent.version)
            .join(Agent, Agent.agent_id == SubmissionImageBuild.agent_id)
            .where(
                SubmissionImageBuild.environment == environment,
                SubmissionImageBuild.updated_at >= cutoff,
            )
            .order_by(
                SubmissionImageBuild.updated_at.desc(),
                SubmissionImageBuild.build_id.desc(),
            )
            .limit(_SUBMISSION_BUILD_LIMIT)
        )
    ).all()
    return PublicSubmissionImageBuildSnapshot(
        window_hours=int(_SUBMISSION_BUILD_WINDOW.total_seconds() // 3600),
        active_count=int(active_count),
        targon_completed_count=int(targon_completed_count),
        fallback_authorized_count=int(fallback_authorized_count),
        builds=[
            PublicSubmissionImageBuild(
                agent_id=row.agent_id,
                agent_name=agent_name,
                agent_version=agent_version,
                status=cast(Any, row.status),
                provider=cast(Literal["targon"] | None, row.provider),
                attempt_count=row.attempt_count,
                output_sha256=row.output_sha256,
                output_size_bytes=row.output_size_bytes,
                error_code=row.error_code,
                created_at=row.created_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
                consumed_at=row.consumed_at,
                updated_at=row.updated_at,
            )
            for row, agent_name, agent_version in recent
        ],
    )


@router.get("/operations", response_model=PublicOperationsResponse)
async def operations(
    request: Request,
    response: Response,
    session: SessionDep,
) -> PublicOperationsResponse:
    """Atomic dashboard snapshot for submission pipeline and validator fleet health."""
    # Match the validator's five-second progress cadence. Holding this atomic
    # snapshot longer made a perfectly normal first-report hand-off look frozen
    # after the signed progress heartbeat had already landed.
    response.headers["Cache-Control"] = _OPERATIONS_CACHE_CONTROL
    now = datetime.now(UTC)
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    if efficiency_config.enabled:
        await ensure_efficiency_state(session, efficiency_config, now=now)
    heartbeat_rows = await list_validator_heartbeats(session)
    benchmark_rollout = await rollout_state(session, now=now, heartbeats=heartbeat_rows)
    active_version = cast(int, benchmark_rollout["active_version"])
    assignments = await list_active_validator_assignments(session, now=now)
    active_work = await list_active_validator_work(
        session,
        now=now,
        cutoff=now - _VALIDATOR_ONLINE_WINDOW,
        heartbeat_rows=heartbeat_rows,
        assignments=assignments,
    )
    confirmation_work = await list_active_confirmation_work(session, now=now)
    (
        score_continuation_floor,
        provisional_contender_floor,
    ) = await get_score_priority_floors(
        session,
        bench_version=active_version,
        efficiency_config=efficiency_config,
        now=now,
        active_version=active_version,
    )
    active_assignment_agent_ids = {
        assignment.agent.agent_id
        for assignment in assignments
        if assignment.ticket.bench_version == active_version
    }
    active_validation_agent_ids = {
        work.agent.agent_id
        for work in active_work
        if work.ticket.bench_version == active_version
    }
    release_policy = await artifact_release_policy(session)
    downloadable_agent_ids = await available_public_source_agent_ids(
        session,
        quorum=SCORING_QUORUM,
        policy=release_policy,
        now=now,
    )
    activity_page = await query_public_activity_page(
        session,
        bench_version=active_version,
        page=1,
        limit=1,
        requested_statuses=set(),
        downloadable_only=False,
        downloadable_agent_ids=downloadable_agent_ids,
        query=None,
        ath_only=False,
        active_validation_agent_ids=active_validation_agent_ids,
        active_assignment_agent_ids=active_assignment_agent_ids,
        score_continuation_floor=score_continuation_floor,
        operations_terminal_history_limit=50,
    )
    activity_rows = activity_page.rows
    activity_statuses = {
        row.agent.agent_id: cast(str, row.public_status) for row in activity_rows
    }
    retry_states = await classify_agent_retry_states(
        session,
        agents=[row.agent for row in activity_rows],
        now=now,
        canonical_version=active_version,
    )
    artifact_releases = await _artifact_release_snapshot(
        session,
        statuses={row.agent.agent_id: row.agent.status for row in activity_rows},
        now=now,
        policy=release_policy,
    )
    ath_reviews, ath_composite = await _ath_review_public_snapshot(
        session, activity_rows
    )
    activity_snapshot = _public_activity_response(
        rows=activity_rows,
        active_work=active_work,
        now=now,
        page=1,
        limit=max(1, activity_page.total),
        requested_statuses=set(),
        downloadable_only=False,
        query=None,
        score_continuation_floor=score_continuation_floor,
        active_assignment_agent_ids=active_assignment_agent_ids,
        artifact_releases=artifact_releases,
        # The board renders the "up next" badge off this ranking, so it must go
        # through the same shared preview ``/activity`` does. It did not, and a
        # stranded previous-generation backlog kept holding the head of the
        # miner-facing queue after that was fixed on the other endpoint.
        queue_preview=await queue_preview_for_rows(
            session,
            rows=activity_rows,
            statuses=activity_statuses,
            bench_version=active_version,
            now=now,
            score_continuation_floor=score_continuation_floor,
            provisional_contender_floor=provisional_contender_floor,
            rollout=activity_page.activated_rollout,
            waiting_agent_ids=activity_page.waiting_agent_ids,
        ),
        active_bench_version=active_version,
        retry_states=retry_states,
        duplicate_metadata=await _duplicate_submission_metadata(session, activity_rows),
        ath_reviews=ath_reviews,
        ath_review_composite=ath_composite,
        precomputed_statuses=activity_statuses,
        precomputed_status_counts=activity_page.status_counts,
        precomputed_downloadable_count=activity_page.downloadable_count,
        precomputed_total=activity_page.total,
        already_paginated=True,
        precomputed_page_size=max(1, len(activity_rows)),
    )
    validator_snapshot = _validator_heartbeats_response(
        rows=heartbeat_rows,
        assignments=assignments,
        active_work=active_work,
        confirmation_work=confirmation_work,
        orphaned_leases=await list_orphaned_leases(
            session, now=now, live_slots=_leased_slots(assignments)
        ),
        now=now,
        active_bench_version=active_version,
        slot_settings=await resolve_slot_settings(request.app.state),
    )
    rows_by_agent = {row.agent.agent_id: row for row in activity_rows}
    desired_version = cast(int, benchmark_rollout["desired_version"])
    desired_work: dict[UUID, list[PublicBenchmarkProgress]] = {}
    for work in active_work:
        if work.ticket.bench_version != desired_version:
            continue
        desired_work.setdefault(work.agent.agent_id, []).append(
            _public_benchmark_progress(work, now)
        )
    rollout_queue: list[PublicRolloutQueueEntry] = []
    if desired_version > active_version and benchmark_rollout["status"] in {
        "collecting",
        "blocked_ineligible",
    }:
        for member in benchmark_rollout["members"]:
            agent_id = UUID(member["agent_id"])
            row = rows_by_agent.get(agent_id)
            if row is None:
                continue
            progress = sorted(
                desired_work.get(agent_id, []), key=lambda item: item.slot_id
            )
            retry = retry_states.get(agent_id)
            rollout_queue.append(
                PublicRolloutQueueEntry(
                    agent_id=agent_id,
                    miner_hotkey=row.agent.miner_hotkey,
                    name=row.agent.name,
                    version=row.agent.version,
                    submitted_at=row.agent.created_at,
                    bench_version=desired_version,
                    position=int(member["position"]),
                    status="evaluating" if progress else "waiting_validator",
                    score_count=int(member["score_count"]),
                    quorum=SCORING_QUORUM,
                    # Cohort membership plus its transaction-bound desired-era
                    # dataset is itself queue admission. Before the first ticket
                    # there is no retry row to classify, but the truthful state
                    # is queued rather than unknown.
                    retry_state=retry.state if retry is not None else "queued",
                    retry_after=(
                        retry.earliest_retry_after if retry is not None else None
                    ),
                    active_benchmarks=progress,
                )
            )
    return PublicOperationsResponse(
        generated_at=now,
        active_bench_version=cast(int, benchmark_rollout["active_version"]),
        desired_bench_version=cast(int, benchmark_rollout["desired_version"]),
        benchmark_rollout_status=cast(
            Literal[
                "inactive",
                "collecting",
                "blocked_ineligible",
                "activated",
                "superseded",
            ],
            benchmark_rollout["status"],
        ),
        activity=activity_snapshot,
        rollout_queue=rollout_queue,
        validators=validator_snapshot,
        submission_builds=await _public_submission_build_snapshot(session, now=now),
    )


@router.post(
    "/agent/{agent_id}/dispute",
    response_model=CreateScreeningDisputeResponse,
    status_code=201,
)
async def create_screening_dispute(
    response: Response,
    session: SessionDep,
    agent_id: UUID,
    payload: CreateScreeningDisputeRequest,
) -> CreateScreeningDisputeResponse:
    """Record the submitting hotkey's single appeal of a quarantine rejection."""

    response.headers["Cache-Control"] = "no-store"
    dispute: ScreeningDispute | None = None
    try:
        async with session.begin():
            agent = await session.scalar(
                select(Agent).where(Agent.agent_id == agent_id).with_for_update()
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="submission not found")
            if not _verify_signature(
                agent.miner_hotkey,
                screening_dispute_signing_message(agent_id, payload.message),
                payload.signature,
            ):
                raise HTTPException(
                    status_code=401,
                    detail="signature did not verify against the submitting hotkey",
                )
            if await session.scalar(
                select(ScreeningDispute).where(ScreeningDispute.agent_id == agent_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="this submission has already used its one dispute",
                )
            quarantine = await session.scalar(
                select(ScreeningQuarantine)
                .where(
                    ScreeningQuarantine.agent_id == agent_id,
                    ScreeningQuarantine.status == "resolved",
                    ScreeningQuarantine.resolution == "reject",
                )
                .order_by(ScreeningQuarantine.resolved_at.desc())
                .with_for_update()
            )
            if agent.status != AgentStatus.REJECTED or quarantine is None:
                raise HTTPException(
                    status_code=409,
                    detail="only a rejected quarantine decision can be disputed",
                )
            dispute = ScreeningDispute(
                dispute_id=uuid4(),
                agent_id=agent.agent_id,
                quarantine_id=quarantine.quarantine_id,
                miner_hotkey=agent.miner_hotkey,
                message=payload.message,
                status="pending",
                created_at=datetime.now(UTC),
            )
            session.add(dispute)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="this submission has already used its one dispute",
        ) from exc

    assert dispute is not None
    return CreateScreeningDisputeResponse(dispute=_public_dispute(dispute))


@router.get("/agent/{agent_id}/summary", response_model=PublicAgentSummary)
async def agent_summary(
    request: Request,
    response: Response,
    session: SessionDep,
    agent_id: UUID,
) -> PublicAgentSummary:
    """Return only the fields needed to paint an agent card at a glance.

    This route is the direct-link hot path. It deliberately avoids the global
    activity population, queue preview, artifact-release calculation, and full
    attempt/score history. Those remain available from the independent pipeline
    route, which clients may load concurrently without blocking this response.
    """
    response.headers["Cache-Control"] = "public, max-age=10"
    now = datetime.now(UTC)
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    if efficiency_config.enabled:
        await ensure_efficiency_state(session, efficiency_config, now=now)
    active_version = await active_bench_version(session)
    row = await get_public_activity_by_id(
        session,
        agent_id=agent_id,
        bench_version=active_version,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="submission not found")

    active_work = await list_active_validator_work(
        session,
        now=now,
        cutoff=now - _VALIDATOR_ONLINE_WINDOW,
        agent_id=agent_id,
    )
    assignments = await list_active_validator_assignments(
        session,
        now=now,
        agent_id=agent_id,
    )
    active_assignment_agent_ids = {
        assignment.agent.agent_id
        for assignment in assignments
        if assignment.ticket.bench_version == active_version
    }
    needs_queue_state = row.agent.status in {
        AgentStatus.SCREENING_PASSED,
        AgentStatus.EVALUATING,
    }
    admitted = (
        await agent_is_admitted(
            session,
            bench_version=active_version,
            agent_id=agent_id,
        )
        if needs_queue_state
        else True
    )
    retired = (
        agent_id in await retired_agent_ids(session, agent_ids={agent_id})
        if needs_queue_state
        else False
    )
    score_floor = (
        await get_score_continuation_floor(
            session,
            bench_version=active_version,
            efficiency_config=efficiency_config,
            now=now,
        )
        if row.agent.status == AgentStatus.EVALUATING
        and row.score_count == SCORING_QUORUM - 1
        else None
    )
    status = _public_activity_status(
        row.agent.status,
        screening_policy_version=row.agent.screening_policy_version,
        has_active_attempt=row.screening_attempt is not None,
        has_active_validation=any(
            work.ticket.bench_version == active_version for work in active_work
        ),
        has_live_assignment=agent_id in active_assignment_agent_ids,
        score_count=row.score_count,
        highest_composite=row.highest_composite,
        score_continuation_floor=score_floor,
        benchmark_admitted=admitted,
        retired=retired,
    )

    ath_reviews: dict[UUID, _PublicAthReviewSnapshot] = {}
    ath_composites: dict[UUID, float] = {}
    if row.agent.status in {AgentStatus.ATH_PENDING_REVIEW, AgentStatus.BANNED}:
        ath_reviews, ath_composites = await _ath_review_public_snapshot(session, [row])
    duplicate_metadata = (
        await _duplicate_submission_metadata(session, [row])
        if row.agent.duplicate_of is not None
        else {}
    )
    review = ath_reviews.get(agent_id)
    duplicate = (
        duplicate_metadata.get(row.agent.duplicate_of)
        if row.agent.duplicate_of is not None
        else None
    )
    duplicate_name = duplicate[0] if duplicate is not None else None
    duplicate_version = duplicate[1] if duplicate is not None else None
    return PublicAgentSummary(
        generated_at=now,
        agent_id=agent_id,
        miner_hotkey=row.agent.miner_hotkey,
        name=row.agent.name,
        version=row.agent.version,
        status=status,
        submitted_at=row.agent.created_at,
        last_scored_at=_aware(row.last_scored_at),
        score_count=row.score_count,
        score_composite=row.score_composite,
        quorum=SCORING_QUORUM,
        screening_reason=(
            None
            if status in {"waiting_screening", "screening"}
            else row.agent.screening_reason
        ),
        duplicate_of=row.agent.duplicate_of,
        duplicate_name=duplicate_name,
        duplicate_version=duplicate_version,
        review_reason=review.reason if review is not None else row.agent.review_reason,
        review_event=review.event if review is not None else None,
        review_event_at=review.event_at if review is not None else None,
        review_original_reason=review.original_reason if review is not None else None,
        review_opened_at=review.opened_at if review is not None else None,
        preserved_composite=ath_composites.get(agent_id),
        active_benchmarks=[
            _public_benchmark_progress(work, now) for work in active_work
        ],
    )


@router.get("/agent/{agent_id}/pipeline", response_model=PublicSubmissionPipeline)
async def agent_pipeline(
    request: Request,
    response: Response,
    session: SessionDep,
    agent_id: UUID,
) -> PublicSubmissionPipeline:
    """Screening history, validator progress, and accepted scores for a submission.

    Accepted scores are visible before quorum with the seed and version-pinned
    dataset command needed to reproduce them. The canonical aggregate remains
    null until the independent-score quorum is reached.
    """
    response.headers["Cache-Control"] = "public, max-age=10"
    now = datetime.now(UTC)
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    if efficiency_config.enabled:
        await ensure_efficiency_state(session, efficiency_config, now=now)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="submission not found")

    attempts = await list_screening_attempts(session, agent_id=agent_id)
    quarantines = list(
        await session.scalars(
            select(ScreeningQuarantine).where(ScreeningQuarantine.agent_id == agent_id)
        )
    )
    quarantines_by_attempt = {
        quarantine.attempt_id: quarantine for quarantine in quarantines
    }
    resolved_quarantines_by_attempt: dict[
        UUID,
        tuple[
            Literal["release", "rescreen", "reject"] | None,
            datetime | None,
            str | None,
        ],
    ] = {
        quarantine.attempt_id: (
            cast(Literal["release", "rescreen", "reject"], quarantine.resolution),
            quarantine.resolved_at,
            quarantine.resolution_reason,
        )
        for quarantine in quarantines
        if quarantine.status == "resolved"
        and quarantine.resolution in {"release", "rescreen", "reject"}
    }
    public_reviews_by_attempt = {
        attempt.attempt_id: _public_terminal_screening_review(
            quarantines_by_attempt.get(attempt.attempt_id),
            artifact_sha256=agent.sha256,
        )
        for attempt in attempts
    }
    tickets = list(
        await session.scalars(
            select(ValidatorTicket)
            .where(ValidatorTicket.agent_id == agent_id)
            .order_by(
                ValidatorTicket.issued_at.desc(),
                ValidatorTicket.validator_hotkey,
            )
        )
    )
    inference_grants = list(
        await session.scalars(
            select(InferenceGrant)
            .where(InferenceGrant.agent_id == agent_id)
            .order_by(
                InferenceGrant.created_at,
                InferenceGrant.validator_hotkey,
                InferenceGrant.ticket_deadline,
            )
        )
    )
    now = datetime.now(UTC)
    active_work = [
        work
        for work in await list_active_validator_work(
            session, now=now, cutoff=now - _VALIDATOR_ONLINE_WINDOW
        )
        if work.agent.agent_id == agent_id
    ]
    # Keyed by the *ticket* the work belongs to, not just the hotkey. A validator
    # holds one ticket per (agent, bench_version), so keying on the hotkey alone
    # painted its already-finished v2/v3 rows as "Scoring now", with the live v4
    # progress bar attached, the moment it picked up the v4 ticket.
    active_by_ticket = {
        (work.ticket.validator_hotkey, work.ticket.bench_version): work
        for work in active_work
    }
    accepted_scores = list(
        await session.scalars(
            select(Score)
            .where(Score.agent_id == agent_id)
            .order_by(Score.created_at, Score.validator_hotkey)
        )
    )
    canonical_version = await active_bench_version(session)
    # Read every generation before owner reduction, then run the same current
    # official-score resolver and canonical owner dedupe used by ranking/floor
    # authority. The old detail path read the SQL pre-efficiency representative,
    # so a cheaper equal-quality generation could lead the board/ledger while
    # its own family panel still marked its expensive sibling representative.
    current_ledger_rows = await list_eligible_ledger(
        session,
        include_fingerprints=False,
        include_details=False,
        dedupe_owners=False,
    )
    family_bench_version = (
        current_ledger_rows[0].bench_version
        if current_ledger_rows
        else canonical_version
    )
    current_families = await list_submission_family_members(
        session, bench_version=family_bench_version
    )
    agent_family_members = next(
        (
            members
            for members in current_families.values()
            if any(member.agent_id == agent_id for member in members)
        ),
        [],
    )
    if agent_family_members:
        family_agent_ids = {member.agent_id for member in agent_family_members}
        current_ranking_scores = await resolve_ranking_scores(
            session,
            rows=current_ledger_rows,
            bench_version=None,
            efficiency_config=efficiency_config,
            now=now,
        )
        current_representatives = dedupe_owner_rows(
            current_ledger_rows,
            scores=current_ranking_scores,
        )
        official_representative_id = next(
            (
                ledger_row.agent_id
                for ledger_row in current_representatives
                if ledger_row.agent_id in family_agent_ids
            ),
            agent_family_members[0].agent_id,
        )
        submission_family = _public_submission_family(
            agent_family_members,
            representative_agent_id=official_representative_id,
        )
    else:
        submission_family = None
    canonical_scores = [
        score for score in accepted_scores if score.bench_version == canonical_version
    ]
    # The era this submission's progress is *reported* against, which is not
    # always the active one. Counting a closed-generation row against the active
    # version told a miner who got 2 of 3 in v6 that they had "0 of 3", reading
    # as if their accepted work had vanished.
    #
    # Any accepted score in the active version settles it: that submission is
    # current-generation work and its progress is the active era's, even when it
    # also holds a ticket for a version being rolled out ahead of the active one.
    # Only a submission with no foothold in the active era falls back, and then
    # to the same resolution the validator-retry surfaces use -- which correctly
    # keeps a carried-over row on the new era (its live ticket outranks its old
    # scores), so a submission cannot be in one era there and another here.
    era_version = (
        canonical_version
        if canonical_scores
        else resolve_bench_version(
            all_tickets=tickets,
            all_scores=accepted_scores,
            canonical_version=canonical_version,
        )
    )
    era_scores = (
        canonical_scores
        if era_version == canonical_version
        else [score for score in accepted_scores if score.bench_version == era_version]
    )
    confirmation_scores = list(
        await session.scalars(
            select(ConfirmationScore)
            .where(ConfirmationScore.agent_id == agent_id)
            .order_by(
                ConfirmationScore.bench_version,
                ConfirmationScore.created_at,
                ConfirmationScore.validator_hotkey,
                ConfirmationScore.seed,
            )
        )
    )
    # Dataset provenance is PER BENCH VERSION. The agent row carries only the
    # version it was first pinned at, so pairing every score with it published the
    # v2 digest alongside a v3 score -- next to a verification_command that
    # correctly names v3, so the two contradicted each other and anyone verifying
    # a v3 score would render v3 and get a mismatch.
    dataset_sha_by_version = {
        pin.bench_version: pin.sha256
        for pin in await session.scalars(
            select(BenchmarkDataset).where(BenchmarkDataset.agent_id == agent_id)
        )
    }

    def _score_dataset_sha(score: Score) -> str | None:
        """The digest of the dataset THIS score was graded against."""
        version = _score_bench_version(score)
        if version is None:
            return agent.dataset_sha256
        return dataset_sha_by_version.get(version, agent.dataset_sha256)

    running_attempt = next(
        (attempt for attempt in attempts if attempt.status == "running"), None
    )
    # One read, so the quoted floor and the agent credited with it cannot come
    # from two different snapshots of a ledger that moves between calls.
    score_floor = await get_score_continuation_floor_row(
        session,
        bench_version=canonical_version,
        efficiency_config=efficiency_config,
        now=now,
    )
    score_continuation_floor = score_floor.score if score_floor is not None else None
    score_floor_agent = (
        await session.get(Agent, score_floor.row.agent_id)
        if score_floor is not None
        else None
    )
    benchmark_admitted = await agent_is_admitted(
        session, bench_version=canonical_version, agent_id=agent_id
    )
    # Retirement is per (agent, closed era), so ask without pinning a version:
    # the detail page must report the state no matter which era it names, and a
    # retired submission stays reachable by direct URL by design.
    agent_retired = agent_id in await retired_agent_ids(session)
    dispute = await session.scalar(
        select(ScreeningDispute).where(ScreeningDispute.agent_id == agent_id)
    )
    return PublicSubmissionPipeline(
        generated_at=now,
        agent_id=agent_id,
        artifact_release=(
            await _artifact_release_snapshot(
                session,
                statuses={agent_id: agent.status},
                now=now,
            )
        )[agent_id],
        status=_public_activity_status(
            agent.status,
            screening_policy_version=agent.screening_policy_version,
            has_active_attempt=running_attempt is not None,
            has_active_validation=any(
                work.ticket.bench_version == canonical_version for work in active_work
            ),
            has_live_assignment=any(
                ticket.status == TicketStatus.ISSUED
                and ticket.bench_version == canonical_version
                and cast(datetime, _aware(ticket.deadline)) > now
                for ticket in tickets
            ),
            # Deliberately the ACTIVE-era count, not ``era_scores``. The only
            # label this feeds is ``below_score_floor``, which compares a score
            # against the active version's continuation floor -- a previous
            # generation's composites are not on that scale, so admitting them
            # here would decide a live-queue question with incomparable numbers.
            score_count=len(canonical_scores),
            highest_composite=(
                max(score.composite for score in canonical_scores)
                if canonical_scores
                else None
            ),
            score_continuation_floor=score_continuation_floor,
            benchmark_admitted=benchmark_admitted,
            retired=agent_retired,
        ),
        submission_family=submission_family,
        active_bench_version=canonical_version,
        score_bench_version=era_version,
        score_count=len(era_scores),
        quorum=SCORING_QUORUM,
        score_floor=score_continuation_floor or 0.0,
        score_floor_agent_id=(
            score_floor.row.agent_id if score_floor is not None else None
        ),
        score_floor_agent_name=(
            score_floor_agent.name if score_floor_agent is not None else None
        ),
        score_floor_agent_version=(
            score_floor_agent.version if score_floor_agent is not None else None
        ),
        provisional_scores=[
            PublicProvisionalScore(
                composite=score.composite,
                raw_composite=(
                    float(score.details["raw_composite"])
                    if isinstance(score.details, dict)
                    and isinstance(score.details.get("raw_composite"), (int, float))
                    and not isinstance(score.details.get("raw_composite"), bool)
                    else None
                ),
                token_usage=(
                    _safe_token_usage(score.details)
                    if isinstance(score.details, dict)
                    else None
                ),
                token_efficiency=(
                    _safe_token_efficiency(score.details)
                    if isinstance(score.details, dict)
                    else None
                ),
                composite_breakdown=_composite_breakdown(
                    tool_mean=score.tool_mean,
                    memory_mean=score.memory_mean,
                    final_composite=score.composite,
                    details=(score.details if isinstance(score.details, dict) else {}),
                ),
                v9_base=(
                    _safe_public_v9_base(score.details)
                    if _score_bench_version(score) == 9
                    and isinstance(score.details, dict)
                    else None
                ),
                calibration_brier=_safe_calibration(
                    score.details if isinstance(score.details, dict) else {}
                )[0],
                calibration_n=_safe_calibration(
                    score.details if isinstance(score.details, dict) else {}
                )[1],
                seed=str(score.seed),
                run_size=agent.dataset_run_size,
                bench_version=_score_bench_version(score),
                datagen_version=_datagen_version(_score_bench_version(score)),
                seed_source=(
                    # No pinned dataset (generation disabled when this agent was
                    # screened): the platform never derived a seed, so the one on
                    # the score is the validator's own benchmark seed.
                    "validator_local"
                    if agent.dataset_seed is None
                    else "on_chain"
                    if agent.dataset_seed_block is not None
                    and agent.dataset_seed_block_hash is not None
                    else "random_fallback"
                ),
                dataset_sha256=_score_dataset_sha(score),
                accepted_at=score.created_at,
                reproduction_command=_dataset_command(
                    seed=score.seed,
                    run_size=agent.dataset_run_size,
                    bench_version=_score_bench_version(score),
                    sha_only=False,
                ),
                verification_command=_dataset_command(
                    seed=score.seed,
                    run_size=agent.dataset_run_size,
                    bench_version=_score_bench_version(score),
                    sha_only=True,
                ),
                case_results=_safe_case_results(
                    score.details if isinstance(score.details, dict) else {}
                ),
                transcript_sha256=_safe_transcript_sha256(
                    score.details if isinstance(score.details, dict) else {}
                ),
            )
            for score in accepted_scores
        ],
        confirmation_scores=[
            PublicConfirmationScore(
                composite=score.composite,
                seed=str(score.seed),
                validator_hotkey=score.validator_hotkey,
                bench_version=score.bench_version,
                accepted_at=score.created_at,
            )
            for score in confirmation_scores
        ],
        # Same era as ``score_count`` above, or the page contradicts itself: a
        # finalized v6 row would read "3 of 3" with no final score to show for
        # it. The median is over one era's scores either way, so this stays the
        # median that submission was actually finalized at -- it is not a second
        # aggregate, and nothing here feeds weights or the leaderboard.
        final_composite=(
            statistics.median(score.composite for score in era_scores)
            if len(era_scores) >= SCORING_QUORUM
            and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
            else None
        ),
        screening_attempts=[
            PublicScreeningAttempt(
                attempt_id=attempt.attempt_id,
                policy_version=attempt.policy_version,
                status=attempt.status,
                screener_hotkey=attempt.screener_hotkey,
                started_at=attempt.started_at,
                deadline=attempt.deadline,
                finished_at=attempt.finished_at,
                reason=attempt.public_reason,
                quarantine_resolution=resolved_quarantines_by_attempt.get(
                    attempt.attempt_id, (None, None, None)
                )[0],
                quarantine_resolved_at=resolved_quarantines_by_attempt.get(
                    attempt.attempt_id, (None, None, None)
                )[1],
                quarantine_resolution_reason=resolved_quarantines_by_attempt.get(
                    attempt.attempt_id, (None, None, None)
                )[2],
                review_evidence=public_reviews_by_attempt[attempt.attempt_id][0],
                review_finding=public_reviews_by_attempt[attempt.attempt_id][1],
            )
            for attempt in attempts
        ],
        validation_attempts=[
            PublicValidationAttempt(
                validator_hotkey=ticket.validator_hotkey,
                status=ticket.status.value,
                purpose=ticket.purpose,
                issued_at=ticket.issued_at,
                deadline=ticket.deadline,
                bench_version=ticket.bench_version,
                actively_running=(ticket.validator_hotkey, ticket.bench_version)
                in active_by_ticket,
                benchmark_progress=(
                    _public_benchmark_progress(
                        active_by_ticket[
                            (ticket.validator_hotkey, ticket.bench_version)
                        ],
                        now,
                    )
                    if (ticket.validator_hotkey, ticket.bench_version)
                    in active_by_ticket
                    else None
                ),
                failure_reason=cast(
                    Literal["infrastructure", "scoring_error", "sandbox_oom"] | None,
                    ticket.failure_reason,
                ),
                failure_code=(
                    cast(
                        Literal[
                            "inference_allowance_exhausted",
                            "model_inference_required",
                        ],
                        ticket.failure_detail,
                    )
                    if ticket.failure_detail
                    in {"inference_allowance_exhausted", "model_inference_required"}
                    else None
                ),
                failed_at=ticket.failed_at,
                attempt_count=ticket.attempt_count,
            )
            for ticket in tickets
        ],
        inference_runs=[
            PublicInferenceRun(
                validator_hotkey=grant.validator_hotkey,
                bench_version=grant.bench_version,
                ticket_deadline=grant.ticket_deadline,
                status=cast(
                    Literal["pending", "active", "revoked", "exhausted"],
                    grant.status,
                ),
                request_budget=grant.request_budget,
                requests=grant.request_count,
                prompt_tokens=grant.prompt_tokens,
                completion_tokens=grant.completion_tokens,
                token_budget=grant.token_budget,
                embedding_requests=grant.embedding_request_count,
                embedding_tokens=grant.embedding_tokens,
                cost_microusd=grant.cost_microusd + grant.embedding_cost_microusd,
                accounting_version=grant.usage_accounting_version,
                created_at=grant.created_at,
                updated_at=grant.updated_at,
            )
            for grant in inference_grants
        ],
        dispute=_public_dispute(dispute) if dispute is not None else None,
    )


@router.get("/submissions", response_model=PublicSubmissionsResponse)
async def submissions(
    response: Response,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> PublicSubmissionsResponse:
    """Recent finalized submissions, most recently scored first.

    The index over the k=3 transparency records: each entry carries the median
    composite, how many validators scored it, and the dataset pin (seed +
    sha256); drill into ``/public/agent/{agent_id}/scores`` for the full
    per-validator breakdown. Held-for-review and still-evaluating agents are
    excluded: only settled public scores appear.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = await list_public_submissions(session, limit=limit)
    now = datetime.now(UTC)
    artifact_releases = await _artifact_release_snapshot(
        session,
        statuses={row.agent_id: row.status for row in rows},
        now=now,
    )
    return PublicSubmissionsResponse(
        generated_at=now,
        count=len(rows),
        quorum=SCORING_QUORUM,
        submissions=[
            _submission_summary(row, artifact_release=artifact_releases[row.agent_id])
            for row in rows
        ],
    )


@router.get("/agent/{agent_id}/scores", response_model=PublicSubmissionScores)
async def agent_scores(
    response: Response,
    session: SessionDep,
    agent_id: UUID,
) -> PublicSubmissionScores:
    """The full k=3 scoring record for one finalized agent.

    Publishes which validators scored the agent, each one's exact numbers +
    signature (self-verifying against the published validator key), the median
    composite the platform finalized on, and the pinned dataset (seed + sha256)
    so anyone can reproduce and audit the score. 404 for an agent that does not
    exist or has not settled into a public status (still evaluating, or held for
    copy review): a provisional agent's partial scores are never exposed.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    row = await get_submission_scores(session, agent_id=agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no public scores for this agent")
    artifact_release = (
        await _artifact_release_snapshot(
            session, statuses={agent_id: row.status}, now=datetime.now(UTC)
        )
    )[agent_id]
    v9_confirmation = (
        await v9_confirmation_public_projections(session, agent_ids=[agent_id])
    ).get(agent_id)
    return _submission_scores(
        row,
        artifact_release=artifact_release,
        v9_confirmation=v9_confirmation,
    )


@router.get("/agent/{agent_id}/artifact", response_model=PublicArtifactDownload)
async def agent_artifact(
    request: Request,
    response: Response,
    session: SessionDep,
    storage: StorageDep,
    agent_id: UUID,
) -> PublicArtifactDownload:
    """Return a short-lived source URL after the configured cleared-score embargo."""
    response.headers["Cache-Control"] = "private, no-store"
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE):
        raise HTTPException(status_code=404, detail="source is not publicly available")

    now = datetime.now(UTC)
    # Re-read the live policy on the request that is about to mint a public
    # credential, rather than trusting the batch projection. This is the only
    # place in the platform that hands out a public URL for miner source, so it
    # is the one place the policy has to be checked against the database and
    # not against a value computed for a different response.
    #
    # 403, not 404 and not 425. 404 would deny the submission exists (it does,
    # and the rest of its record is public). 425 means "too early" and invites
    # a retry that will never succeed under this policy.
    policy = await artifact_release_policy(session)
    if not policy.releases_publicly:
        raise HTTPException(
            status_code=403,
            detail=(
                "subnet policy withholds all submitted source from public "
                "release; no source is published, however long you wait"
            ),
        )

    score_quorum = (
        await list_first_score_quorums(
            session, agent_ids=[agent_id], quorum=SCORING_QUORUM
        )
    ).get(agent_id)
    king_reveal = (await get_king_reveal(session, agent_ids=[agent_id])).get(agent_id)
    # King-only: an agent that has never held the crown is never revealed.
    if king_reveal is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "source is not publicly available; only the king's source is released"
            ),
        )
    release = _public_artifact_release(
        status=agent.status,
        score_quorum=score_quorum,
        policy=policy,
        king_reveal=king_reveal,
        now=now,
    )
    if release.status != "available" or score_quorum is None:
        detail = "source is awaiting a three-validator score quorum"
        if king_reveal.weight_confirmed_at is None:
            detail = (
                "source is awaiting on-chain confirmation that validator weights "
                "were set on this king (commit-reveal)"
            )
        elif release.available_at is not None:
            detail = f"source is embargoed until {release.available_at.isoformat()}"
        raise HTTPException(status_code=425, detail=detail)

    download_url = await storage.presigned_get_url(
        key=f"{agent_id}/agent.tar.gz",
        expires_in=_ARTIFACT_DOWNLOAD_TTL_SECONDS,
        attachment_filename=f"ditto-agent-{agent_id}.tar.gz",
    )
    # Audited only once the release gate above has passed, so a row means source
    # was actually handed out. That is also what keeps this unauthenticated route
    # from being a write-amplification lever: a caller cannot make the platform
    # insert rows without first getting past the king-reveal and embargo checks.
    # There is no requester identity to record here by design -- the peer address
    # is the only discriminator the route has.
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_PUBLIC_ARTIFACT,
        requester_kind="public",
        bench_version=score_quorum.bench_version,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(
            request,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        ),
    )
    return PublicArtifactDownload(
        agent_id=agent.agent_id,
        bench_version=score_quorum.bench_version,
        sha256=agent.sha256,
        finalized_at=score_quorum.finalized_at,
        download_url=download_url,
        expires_at=now + timedelta(seconds=_ARTIFACT_DOWNLOAD_TTL_SECONDS),
    )


@router.get("/agent/{agent_id}/dataset", response_model=PublicDatasetReveal)
async def agent_dataset(
    response: Response,
    session: SessionDep,
    generator: GeneratorDep,
    agent_id: UUID,
) -> PublicDatasetReveal:
    """The FULL labeled dataset a finalized submission was scored against.

    Regenerated from the submission's published (on-chain-derived) seed so anyone
    can independently re-grade its k=3 scores. The regenerated artifact's SHA-256
    is re-verified against the hash pinned at scoring, so the revealed bytes
    provably are the scored dataset. 404 for an unknown / not-yet-finalized agent
    (a provisional agent's answers are never revealed); 502 if the generator drifts
    from the pinned hash; 503 if the generate service is unavailable.

    Safe despite carrying the answer key: the seed is one-time and was
    unpredictable at submission, so a past dataset's answers cannot help overfit a
    future (differently-seeded) run.
    """
    # A finalized dataset never changes (fixed seed), so it is immutable + highly
    # cacheable.
    response.headers["Cache-Control"] = "public, max-age=3600, immutable"
    row = await get_submission_scores(session, agent_id=agent_id)
    if row is None or row.dataset_seed is None or row.dataset_run_size is None:
        raise HTTPException(
            status_code=404, detail="no revealable dataset for this agent"
        )
    # The era this agent actually ran, not a default.
    #
    # `fetch_dataset` used to default `bench_version` to 2, and this call
    # omitted it -- so the reveal served the v2 dataset for every finalized
    # agent regardless of the benchmark it was scored under. Silent, because a
    # v2 dataset is a perfectly well-formed artifact; it just is not this
    # agent's. The retired-era floor made the default unreachable and the bug
    # visible.
    #
    # The agent's own scores are the authority on which era finalized it, so
    # take the newest of them. A submission reaching this endpoint is finalized
    # and therefore has scores; fall back to the active era rather than assume.
    dataset_bench_version = (
        max(score.bench_version for score in row.scores)
        if row.scores
        else await active_bench_version(session)
    )
    try:
        artifact, sha = await generator.fetch_dataset(
            row.dataset_seed, row.dataset_run_size, dataset_bench_version
        )
    except DataPipelineError as e:
        raise HTTPException(
            status_code=503, detail="dataset generate service unavailable"
        ) from e
    if row.dataset_sha256 and sha.lower() != row.dataset_sha256.lower():
        # The regenerated dataset does not hash to what was pinned at scoring,
        # generator drift. Refuse rather than serve a dataset that is not the one
        # that was scored.
        raise HTTPException(
            status_code=502,
            detail="regenerated dataset does not match the pinned hash",
        )
    bench_version = artifact.get("bench_version")
    return PublicDatasetReveal(
        agent_id=row.agent_id,
        miner_hotkey=row.miner_hotkey,
        seed=row.dataset_seed,
        run_size=row.dataset_run_size,
        dataset_sha256=sha,
        bench_version=bench_version if isinstance(bench_version, int) else None,
        dataset_seed_block=row.dataset_seed_block,
        dataset_seed_block_hash=row.dataset_seed_block_hash,
        artifact=artifact,
    )


@router.get("/audit", response_model=PublicAuditResponse)
async def audit(
    response: Response,
    session: SessionDep,
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> PublicAuditResponse:
    """A page of the append-only, hash-chained score audit log, oldest first.

    The tamper-evident public projection of every scoring event: each validator's
    signed ``score`` and each ``agent_finalized`` (quorum reached, the median +
    scoring validators), in append order. Replay from ``since_seq=0`` and
    re-request with the last ``seq`` seen to stream new entries; recompute each
    ``entry_hash`` and check it links to the prior ``prev_hash`` (rooted at
    ``genesis_hash``) to prove nothing was reordered, edited, or dropped. Every
    ``score`` entry also carries the validator's sr25519 signature, so a consumer
    can verify authenticity against the published validator key. Never carries
    per-case answer-key content.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    entries = await list_audit_entries(session, since_seq=since_seq, limit=limit)
    return PublicAuditResponse(
        generated_at=datetime.now(UTC),
        count=len(entries),
        genesis_hash=GENESIS_HASH,
        head_hash=entries[-1].entry_hash if entries else None,
        entries=[
            PublicAuditEntry(
                seq=e.seq,
                agent_id=e.agent_id,
                validator_hotkey=e.validator_hotkey,
                event=e.event,
                payload=e.payload,
                prev_hash=e.prev_hash,
                entry_hash=e.entry_hash,
                recorded_at=e.recorded_at,
            )
            for e in entries
        ],
    )


def _corpus_per_case(details: object) -> list[dict[str, Any]]:
    """Extract the full UNREDACTED per-case list from a score's details blob."""
    if not isinstance(details, dict):
        return []
    per_case = details.get("per_case")
    if not isinstance(per_case, list):
        return []
    return [c for c in per_case if isinstance(c, dict)]


@router.get("/bench/{version}/corpus", response_model=PublicBenchCorpusResponse)
async def bench_corpus(
    response: Response,
    session: SessionDep,
    version: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PublicBenchCorpusResponse:
    """The FULL labeled corpus of a RETIRED benchmark version (answer keys included).

    Once a benchmark is superseded it is never scored again, so its per-case answer
    keys carry zero anti-overfit cost and are released verbatim from the stored
    scores for research + audit. Refused with 409 for the current (live) version or
    any unknown future version, so no live answer key is ever exposed here.
    Paginate with ``limit`` / ``offset`` up to ``total``.
    """
    # Retirement follows the ACTIVATED epoch, not the shipped constant: releasing
    # answer keys is irreversible and must not happen before miners are notified.
    active = await active_bench_version(session)
    if not is_bench_version_retired(version, active):
        raise HTTPException(
            status_code=409,
            detail=(
                f"bench_version {version} is not retired (active is "
                f"{active}); its answer keys are not released"
            ),
        )
    response.headers["Cache-Control"] = "public, max-age=3600, immutable"
    rows, total = await list_scores_for_bench_version(
        session, version=version, limit=limit, offset=offset
    )
    return PublicBenchCorpusResponse(
        bench_version=version,
        generated_at=datetime.now(UTC),
        count=len(rows),
        total=total,
        limit=limit,
        offset=offset,
        entries=[
            PublicBenchCorpusEntry(
                agent_id=score.agent_id,
                miner_hotkey=miner,
                validator_hotkey=score.validator_hotkey,
                seed=score.seed,
                run_id=score.run_id,
                composite=score.composite,
                per_case=_corpus_per_case(score.details),
            )
            for score, miner in rows
        ],
    )


@router.get("/bench/config", response_model=PublicBenchConfigResponse)
async def bench_config(
    response: Response, session: SessionDep
) -> PublicBenchConfigResponse:
    """The active or operator-selected benchmark setup.

    The harness model is a consensus parameter: every scoring validator runs
    the same frozen open-weight artifact through a model-pinning gateway, so
    model choice is not a miner lever and k=3 scores are comparable. Shipping
    support for a future contract does not publish it here: the Backroom rollout
    target selects it. The ``BENCH_*`` env overrides exist for coordinated fleet
    bumps only.
    """
    response.headers["Cache-Control"] = "public, max-age=300"
    active_version = await active_bench_version(session)
    rollout = await open_rollout(session)
    desired_version = rollout.desired_version if rollout is not None else None
    v7_or_newer = active_version >= 7
    default_model = "openai/gpt-oss-20b" if v7_or_newer else "qwen/qwen3-32b"
    default_serving = (
        "OpenRouter dynamic provider route" if v7_or_newer else "Qwen/Qwen3-32B-TEE"
    )
    public_bucket = os.environ.get("STORAGE_PUBLIC_BUCKET", "")
    mirror = (
        f"https://storage.googleapis.com/{public_bucket}/scored/{{agent_id}}.json"
        if public_bucket
        else None
    )
    transcript_template = (
        f"https://storage.googleapis.com/{public_bucket}/transcripts/{{sha256}}.json"
        if public_bucket
        else None
    )
    return PublicBenchConfigResponse(
        bench_version=active_version,
        desired_bench_version=desired_version,
        harness=BenchHarnessConfig(
            locked=True,
            canonical_id=os.environ.get("BENCH_HARNESS_MODEL_ID", default_model),
            serving=os.environ.get("BENCH_HARNESS_SERVING", default_serving),
            thinking=(
                True
                if v7_or_newer
                else os.environ.get("BENCH_HARNESS_THINKING", "false") == "true"
            ),
            reasoning_effort="medium" if v7_or_newer else None,
            enforcement=(
                (
                    "ticket-scoped platform proxy forces the model and medium "
                    "reasoning effort and holds the upstream key outside the "
                    "sandbox; sandbox egress is deny-all"
                )
                if v7_or_newer
                else (
                    "model-pinning relay forces the model field and holds the "
                    "upstream key outside the sandbox; sandbox egress is deny-all "
                    "(no other model is reachable)"
                )
            ),
        ),
        grading=BenchGradingConfig(
            judge_free=True,
            grader="github.com/ditto-assistant/dittobench-datagen/grade",
            description=(
                "deterministic per-answer_kind checks with distractor and "
                "forbidden-value zeroing; a score is a pure function of "
                "(dataset, transcript)"
            ),
        ),
        dataset=BenchDatasetConfig(
            generator="github.com/ditto-assistant/dittobench-datagen",
            seed_derivation=(
                "derived from an on-chain block hash fixed AFTER the miner "
                "commits; unpredictable, one fresh dataset per submission"
            ),
            reproduce=(
                "generate -bench-version <bench_version> -seed <seed> "
                "-run-size full -sha reproduces any scored run's exact bytes "
                "and dataset_sha256"
            ),
        ),
        public_mirror_url_template=mirror,
        public_transcript_url_template=transcript_template,
        public_transcript_telemetry_url_template=(
            "/api/v1/public/bench/transcript/{sha256}/telemetry"
        ),
        ledger_path="/api/v1/scoring/scores",
        generated_at=datetime.now(UTC),
    )


def _telemetry_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _telemetry_nonnegative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _telemetry_outcome(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = re.sub(r"[^a-z0-9_-]", "", value.lower())[:40]
    return normalized or "unknown"


def _public_transcript_telemetry(transcript: Any, *, sha256_hex: str) -> dict[str, Any]:
    """Return the strict public allowlist from a verified private transcript."""
    source = transcript if isinstance(transcript, dict) else {}
    execution_source = source.get("execution")
    execution_source = execution_source if isinstance(execution_source, dict) else {}
    relay_source = source.get("model_relay")
    relay_source = relay_source if isinstance(relay_source, dict) else {}
    execution: dict[str, Any] = {
        key: _telemetry_nonnegative_int(execution_source.get(key))
        for key in (
            "cases",
            "succeeded",
            "timed_out",
            "cancelled",
            "retried",
            "total_attempts",
        )
    }
    execution.update(
        {
            key: _telemetry_nonnegative_number(execution_source.get(key))
            for key in ("median_duration_ms", "p95_duration_ms", "max_duration_ms")
        }
    )
    model_relay = {
        key: _telemetry_nonnegative_int(relay_source.get(key))
        for key in (
            "requests",
            "successes",
            "infrastructure_failures",
            "caller_cancellations",
            "upstream_attempts",
            "retries",
            "route_probe_attempts",
            "route_probe_routed",
        )
    }
    cases: list[dict[str, Any]] = []
    case_sources = source.get("cases")
    if isinstance(case_sources, list):
        for position, item in enumerate(case_sources[:500], start=1):
            item = item if isinstance(item, dict) else {}
            case_execution = item.get("execution")
            case_execution = case_execution if isinstance(case_execution, dict) else {}
            attempts: list[dict[str, Any]] = []
            attempt_sources = case_execution.get("attempts")
            if isinstance(attempt_sources, list):
                for attempt_position, attempt in enumerate(
                    attempt_sources[:12], start=1
                ):
                    attempt = attempt if isinstance(attempt, dict) else {}
                    status = attempt.get("http_status")
                    attempts.append(
                        {
                            "attempt": _telemetry_nonnegative_int(
                                attempt.get("attempt")
                            )
                            or attempt_position,
                            "duration_ms": _telemetry_nonnegative_number(
                                attempt.get("duration_ms")
                            ),
                            "outcome": _telemetry_outcome(attempt.get("outcome")),
                            "http_status": (
                                status
                                if isinstance(status, int)
                                and not isinstance(status, bool)
                                and 100 <= status <= 599
                                else None
                            ),
                        }
                    )
            cases.append(
                {
                    "position": position,
                    "total_duration_ms": _telemetry_nonnegative_number(
                        case_execution.get("total_duration_ms")
                    ),
                    "terminal_outcome": _telemetry_outcome(
                        case_execution.get("terminal_outcome")
                    ),
                    "timed_out": case_execution.get("timed_out") is True,
                    "cancelled": case_execution.get("cancelled") is True,
                    "attempts": attempts,
                }
            )
    return {
        "source_sha256": sha256_hex,
        "execution": execution,
        "model_relay": model_relay,
        "cases": cases,
    }


@router.get("/bench/transcript/{sha256_hex}/telemetry")
async def public_bench_transcript_telemetry(
    sha256_hex: str,
    storage: StorageDep,
    response: Response,
) -> dict[str, Any]:
    """Return safe metrics from one immutable, signature-bound transcript.

    The digest is already public in the signed score. Restricting reads to the
    content-addressed transcript namespace prevents this endpoint from becoming
    a general storage proxy. The stored bytes are hashed again before parsing,
    then projected through a strict allowlist so no transcript content, raw
    errors, prompts, responses, or tool payloads become public.
    """
    if not _TRANSCRIPT_SHA256_RE.fullmatch(sha256_hex):
        raise HTTPException(status_code=404, detail="transcript not found")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    key = f"transcripts/{sha256_hex}.json"
    try:
        body = await storage.get_object(key=key, max_bytes=_TRANSCRIPT_MAX_BYTES)
    except ObjectDownloadFailedError:
        raise HTTPException(status_code=404, detail="transcript not found") from None
    if hashlib.sha256(body).hexdigest() != sha256_hex:
        logger.error("stored transcript digest mismatch for %s", sha256_hex)
        raise HTTPException(status_code=502, detail="transcript integrity check failed")
    try:
        transcript = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=502, detail="transcript telemetry could not be decoded"
        ) from None
    return _public_transcript_telemetry(transcript, sha256_hex=sha256_hex)


@router.get("/bench/glossary", response_model=PublicBenchGlossaryResponse)
async def bench_glossary(response: Response) -> PublicBenchGlossaryResponse:
    """Every scored category and every metric / composite-gate factor, explained.

    So miners understand exactly what a score reflects: what each test category
    probes (never the answer key) and how each headline metric and gate factor is
    computed. This is the programmatic source for the dashboard's category and
    metric glossaries, and it names the quality factors (conversational-sanity,
    metamorphic-consistency, tool-efficiency) folded into the composite breakdown.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return PublicBenchGlossaryResponse(
        bench_version=CURRENT_BENCH_VERSION,
        categories=[
            PublicCategoryDoc(**c) for c in bench_glossary_data.category_entries()
        ],
        metrics=[PublicMetricDoc(**m) for m in bench_glossary_data.metric_entries()],
        versions=[
            PublicBenchVersionDoc(**v) for v in bench_glossary_data.version_entries()
        ],
    )


@router.get("/bench/rollout")
async def benchmark_rollout_state(
    response: Response, session: SessionDep, generator: GeneratorDep
) -> PublicBenchRolloutResponse:
    """Expose desired/active versions and the frozen cohort's exact progress.

    ``ranked_quorum_agents`` / ``min_ranked_quorum_agents`` answer the question
    the rest of this payload only implies: how close the desired version is to
    taking over weight-setting. Weights stay on ``active_version`` until the
    former reaches the latter.
    """
    response.headers["Cache-Control"] = "public, max-age=30"
    state = await rollout_state(session)
    state["qualification_blockers"] = await rolling_qualification_blockers(
        session, generator_run_size=generator.run_size
    )
    return PublicBenchRolloutResponse.model_validate(state)

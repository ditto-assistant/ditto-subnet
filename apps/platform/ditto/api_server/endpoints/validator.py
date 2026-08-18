"""Validator-facing endpoints — the daemon's epoch loop against the platform.

The platform is intentionally *thin*: the validator daemon owns the chain
identity and drives the scoring engine (``dittobench-api``) itself. These
endpoints let it (1) pull agents awaiting evaluation, (2) fetch the uploaded
tarball, and (3) report a DittoBench :class:`ScoreReport` back. Weight-setting
stays on the daemon (``ChainClient.put_weights``); the platform never touches
the chain identity.

Lifecycle + scope decisions (documented so they're easy to revisit):

- **Queue = agents in ``evaluating``.** Honors the partial index
  ``agents_status_evaluating_idx``. The screener promotes ``uploaded ->
  evaluating`` (see ``endpoints/screener.py``); a submission that hasn't been
  screened yet won't appear here.
- **Scoring is k=3 multi-validator consensus.** Up to
  :data:`~ditto.db.queries.scores.SCORING_QUORUM` distinct validators each score
  an agent, gated by leased tickets (:mod:`ditto.db.queries.tickets`), one row
  per ``(agent, validator)``. The agent stays ``evaluating`` until the
  quorum-th score, then the handler finalizes it on the **median** composite and
  transitions ``evaluating -> scored`` (or ``ath_pending_review`` if the copy
  gate holds it). No single validator is decisive; the transition lives in one
  place (:data:`_SCOREABLE_STATUSES` + the handler).
- **Auth.** Only chain-registered hotkeys holding a ``validator_permit`` may
  call these. Job claims additionally carry a fresh, one-time signed nonce so a
  caller cannot reserve work by merely naming somebody else's permitted
  hotkey. The score POST verifies an sr25519 signature over a
  **canonical payload** binding the agent id and the reported
  ``run_id`` / ``composite`` / ``seed`` (see :func:`_score_signing_message`), so
  a captured signature can neither be replayed against a different agent nor
  cover an altered composite. The remaining GET endpoints are read-only and
  authenticate via the ``X-Validator-Hotkey`` header + on-chain permit check;
  they cannot allocate a quorum slot or submit a score.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import statistics
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import UUID, uuid4

import bittensor
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import (
    ArtifactResponse,
    BenchmarkProgress,
    FailJobRequest,
    FailJobResponse,
    JobRequest,
    JobResponse,
    ScoreReport,
    SubmitScoreRequest,
    SubmitScoreResponse,
    SubmitTranscriptResponse,
    Top5ConfirmationJobRequest,
    ValidatorHeartbeatRequest,
    ValidatorHeartbeatResponse,
)
from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES, AgentStatus
from ditto.api_models.benchmark_capacity import (
    BenchmarkCapacity,
    benchmark_capacity_signing_token,
)
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.benchmark_progress import benchmark_progress_signing_token
from ditto.api_models.confirmation_bundles import supports_confirmation
from ditto.api_models.confirmation_progress import (
    ConfirmationProgress,
    confirmation_progress_signing_token,
)
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.inference import InferenceGrantOffer
from ditto.api_models.queue_policy_settings import (
    DeferredSourceReviewSettings,
    PrevGenCarryoverSettings,
    QueuePolicySettings,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.stack_health import (
    ValidatorStackHealth,
    validator_stack_health_signing_token,
)
from ditto.api_models.system_health import (
    SystemMetrics,
    system_metrics_signing_token,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_models.validator import ConfirmationDatasetPin, HeldLease, V9BaseEvidence
from ditto.api_models.validator_capabilities import (
    ValidatorCapabilities,
    ValidatorStackIdentity,
    validator_artifact_mode,
    validator_identity_signing_token,
)
from ditto.api_models.validator_slot_settings import (
    HARD_SLOT_CEILING,
    ValidatorSlotSettings,
)
from ditto.api_models.validator_updater import (
    ValidatorUpdaterStatus,
    validator_updater_status_signing_token,
)
from ditto.api_server.anti_copy_comparison import ANTI_COPY_ALGORITHM_VERSION
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.benchmark_rollout import (
    refresh_rolling_qualification,
)
from ditto.api_server.config import ValidatorCompatibilityConfig
from ditto.api_server.confirmation_candidate_reconciliation import (
    reconcile_confirmation_candidates,
)
from ditto.api_server.continual_retest_settings import (
    ContinualRetestSettingsResolver,
    rollout_standdown_reason,
)
from ditto.api_server.crn import (
    bounded_continual_seed_set,
    fold_seed_bound,
)
from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.deferred_source_review import (
    DEFERRED_MECHANICAL_REASON,
    DEFERRED_REVIEW_KIND,
    DEFERRED_REVIEW_REASON,
    DeferredReviewDecision,
    evaluate_deferred_review,
)
from ditto.api_server.dependencies import (
    get_chain_client,
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.efficiency import (
    audited_v9_run_token_total,
    ensure_current_efficiency_state,
)
from ditto.api_server.endpoints.retrieval import AgentNotFoundError
from ditto.api_server.fingerprint import reference_corpus_provenance
from ditto.api_server.inference_concurrency_settings import resolved_proxy_config
from ditto.api_server.inference_routing import record_ticket_route_quality
from ditto.api_server.koth import (
    KothEntry,
    continual_composite,
    effective_composite,
    emission_set,
    project_koth,
    retest_cohort,
    top5_round_is_due,
)
from ditto.api_server.model_use import evaluate_model_use, model_use_policy
from ditto.api_server.onchain_seed import derive_validator_seed
from ditto.api_server.outlier_escalation import (
    OUTLIER_ALGORITHM_VERSION,
    OUTLIER_REVIEW_KIND,
    OutlierEscalationSettings,
    evaluate_score_outlier,
)
from ditto.api_server.queue_policy_settings import (
    DEFAULT_SETTINGS as QUEUE_POLICY_DEFAULTS,
)
from ditto.api_server.queue_policy_settings import (
    QueuePolicySettingsResolver,
)
from ditto.api_server.scoring_gate import (
    PublicSourceRelease,
    evaluate_duplicate_signals,
    evaluate_rejected_resubmission,
)
from ditto.api_server.storage import S3StorageClient
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS as SLOT_SETTINGS_DEFAULT,
)
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
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationScore,
    InferenceGrant,
    Score,
    ScreeningAttempt,
    ScreeningQuarantine,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.agents import get_agent_by_id
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_VALIDATOR_ARTIFACT,
    record_artifact_fetch,
)
from ditto.db.queries.artifact_release import list_public_source_releases
from ditto.db.queries.artifact_release_settings import artifact_release_policy_as_of
from ditto.db.queries.attestation import list_linked_hotkeys
from ditto.db.queries.audit import (
    EVENT_AUDIT,
    EVENT_COPY_NO_OPPORTUNITY,
    EVENT_FINALIZED,
    EVENT_SCORE,
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
    get_latest_score_retest_event,
)
from ditto.db.queries.benchmark_admission import activated_rollout_for_version
from ditto.db.queries.benchmark_carryover import carryover_agent_ids
from ditto.db.queries.benchmark_rollout import (
    LEGACY_BENCH_VERSION,
    MIN_SCOREABLE_BENCH_VERSION,
    active_bench_version,
    heartbeat_supports_version,
    issue_rollout_ticket,
    open_rollout,
    rollout_cohort_complete,
)
from ditto.db.queries.confirmation_scores import (
    DEFAULT_WAVE_MEMBERSHIP,
    ConfirmationSeedScore,
    WaveMembership,
    append_confirmation_scores,
    completed_confirmation_wave_seeds,
    confirmation_catchup_seeds,
    confirmation_composites_by_seed,
    fold_eligible_seeds_by_agent,
)
from ditto.db.queries.desired_era_backlog import desired_era_work_outstanding
from ditto.db.queries.heartbeats import (
    HeartbeatProgressRegressionError,
    _validate_same_lease_progress,
    upsert_validator_heartbeat,
)
from ditto.db.queries.inference import (
    ensure_inference_grant,
    get_lease_model_usage,
    revoke_ticket_inference,
    ticket_inference_revoked_mid_lease,
)
from ditto.db.queries.king_reign import (
    list_unconfirmed_kings,
    record_first_crowned,
    record_weight_confirmed,
)
from ditto.db.queries.payments import get_miner_coldkey_for_agent
from ditto.db.queries.retry_budget import (
    INFRA_RETRY_BACKOFF_CAP,
    agent_infra_retry_grants,
    grant_no_fault_retry,
    infra_retry_backoff,
)
from ditto.db.queries.rollout_dispatch import try_lock_rollout_dispatch
from ditto.db.queries.score_ranking import (
    dedupe_owner_rows,
    resolve_efficiency_adjustments,
)
from ditto.db.queries.score_retests import (
    V9_CONTRACT_RETEST_BASIS,
    activate_next_score_retest,
)
from ditto.db.queries.scores import (
    SCORING_QUORUM,
    get_score_for_validator,
    list_anti_copy_history,
    list_eligible_ledger,
    list_rejected_artifacts,
    list_scores_for_agent,
    quorum_composites,
    upsert_score,
)
from ditto.db.queries.similarity_grouping import (
    SimilarityBudgetPolicy,
    policy_from_settings,
)
from ditto.db.queries.tickets import (
    OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    RETRY_COOLDOWN,
    SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    get_open_ticket,
    issue_confirmation_ticket,
    issue_ticket,
    list_live_slot_tickets,
    list_validator_live_leases,
    mark_ticket_scored,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)
from ditto.metrics import (
    VALIDATOR_DISPATCH_DECLINED,
    VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED,
    DispatchDeclineReason,
)
from ditto.score_order import rank_submissions

if TYPE_CHECKING:
    from ditto.api_server.config import EfficiencyBonusConfig, InferenceProxyConfig
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)


def _inference_grant_offer(
    *, request: Request, grant: InferenceGrant, bench_version: int
) -> InferenceGrantOffer:
    """Serialize the same ticket-scoped capability for every scoring lane."""
    public_base_url = request.app.state.config.inference_proxy.public_base_url
    return InferenceGrantOffer(
        grant_id=grant.grant_id,
        exchange_url=f"{public_base_url}/api/v1/inference/exchange",
        proxy_url=f"{public_base_url}/api/v1/inference/chat/completions",
        allowed_models=list(grant.allowed_models),
        request_budget=grant.request_budget,
        token_budget=grant.token_budget,
        expires_at=grant.expires_at,
        provider=grant.route_provider if bench_version >= 7 else None,
        profile_revision=grant.route_profile if bench_version >= 7 else None,
    )


# Reproduce-under-transform audit (v3 Part A). These mirror the validator's
# constants in ditto-subnet ``ditto/validator/transform_audit.py``, which in turn
# mirror dittobench-datagen ``persona/transform.go``. They are part of a PUBLIC
# derivation contract, not tunables: the whole point is that any third party can
# recompute a verdict from the published seed and get the same answer.
AUDIT_BPS = 2500

# The brittleness verdict is a one-sided exact BINOMIAL TEST on discordant audit
# pairs, mirroring ditto-subnet ``ditto/validator/transform_audit.py``.
#
# A pair answered correctly in the base phrasing and incorrectly under the
# post-commit transform is the brittleness event; the mirror image is not. The
# null is that discordant pairs fall either way equally, which is what an honest
# nondeterministic model does. The 2026-07-18 calibration measured honest at 5
# base-only vs 6 transform-only (symmetric) and a surface-gated harness at 6 vs
# 0; the previous ratio threshold could not tell those apart.
#
# ALPHA *is* the false-positive rate on honest miners, by construction. The
# ratio floor it replaces had an unknown error rate, measured at 16% of honest
# runs.
AUDIT_ALPHA = 0.01
# Fewest discordant pairs that can produce a verdict: below this the exact test
# cannot reach ALPHA even on a perfect one-directional run.
AUDIT_MIN_DISCORDANT = 6
TRANSFORM_AUDIT_REVIEW_REASON = "transform_audit_brittleness"

# Enforcement stays OFF by default. The metric now discriminates in principle
# (see the calibration in dittobench-api docs/BASELINES.md Run 3), but the floor
# has not been re-validated end to end against the population it judges --
# champion/tail agents, which are more accurate than the stock reference harness
# every number above came from. Turn this on only with that evidence.
TRANSFORM_AUDIT_ENFORCE = os.environ.get(
    "DITTO_TRANSFORM_AUDIT_ENFORCE", "false"
).strip().lower() in {"1", "true", "yes", "on"}


# Out-of-band composite escalation to ATH review (issue #476). Env-driven like
# the transform-audit toggle above so the safety net can be rolled out
# observe-first and enabled without a code change. Ships in ``off`` mode: the
# gate is a no-op until an operator sets ``DITTO_OUTLIER_ESCALATION_MODE`` to
# ``observe`` (record would-be holds) or ``enforce`` (open them). The bench-
# version floor keeps v8-v11 behaviour byte-identical.
def _outlier_escalation_settings_from_env() -> OutlierEscalationSettings:
    """Build the escalation policy from the environment, falling back to shipped
    defaults for any variable that is unset or unparseable (fail-safe: a bad
    value degrades to the conservative default rather than crashing scoring)."""
    defaults = OutlierEscalationSettings()

    mode = (
        os.environ.get("DITTO_OUTLIER_ESCALATION_MODE", defaults.mode).strip().lower()
    )
    if mode not in {"off", "observe", "enforce"}:
        mode = defaults.mode

    def _int(name: str, fallback: int) -> int:
        try:
            return int(os.environ[name])
        except (KeyError, ValueError):
            return fallback

    def _float(name: str, fallback: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return fallback

    return OutlierEscalationSettings(
        mode=mode,
        min_bench_version=_int(
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION", defaults.min_bench_version
        ),
        min_cohort_size=_int(
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE", defaults.min_cohort_size
        ),
        modified_z_threshold=_float(
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD",
            defaults.modified_z_threshold,
        ),
        min_composite_floor=_float(
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR",
            defaults.min_composite_floor,
        ),
    )


OUTLIER_ESCALATION_SETTINGS = _outlier_escalation_settings_from_env()


def _binomial_tail(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact, no dependencies."""
    if n <= 0:
        return 1.0
    k = max(0, k)
    total = 0.0
    coeff = 1.0
    for i in range(0, n + 1):
        if i >= k:
            total += coeff * (p**i) * ((1 - p) ** (n - i))
        coeff = coeff * (n - i) / (i + 1)
    return min(1.0, total)


def _pool_audit_pairs(agent_scores: Sequence[Any]) -> dict[str, int]:
    """Sum the audit 2x2 counts across an agent's finalized scores.

    Each validator already pooled its own confirmation runs; this pools across
    the k=3 validators, so the verdict rests on all the evidence rather than on
    any one validator's handful of pairs. Same reasoning as finalizing the
    composite on the median: no single validator decides an agent's fate.
    """
    pooled = {"both_correct": 0, "base_only": 0, "transform_only": 0, "both_wrong": 0}
    for score in agent_scores:
        details = score.details if isinstance(score.details, dict) else {}
        raw = details.get("audit_pairs_pooled") or details.get("audit_pairs")
        if not isinstance(raw, dict):
            continue
        for key in pooled:
            v = raw.get(key, 0)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                pooled[key] += v
    return pooled


def _transform_audit_verdict(
    agent_scores: Sequence[Any],
) -> tuple[float | None, dict[str, int], bool]:
    """Pooled brittleness verdict across an agent's finalized scores.

    Returns ``(p_value, pooled_counts, failed)``. ``failed`` is False whenever
    the evidence is thin -- no score carried the counts (an older scoring
    engine), or too few discordant pairs to reach ALPHA. Absence of evidence is
    not a failed audit, and the cost of getting that backwards is paid by a
    legitimate miner.
    """
    pooled = _pool_audit_pairs(agent_scores)
    discordant = pooled["base_only"] + pooled["transform_only"]
    if sum(pooled.values()) == 0:
        return None, pooled, False
    if discordant < AUDIT_MIN_DISCORDANT:
        return None, pooled, False
    pvalue = _binomial_tail(pooled["base_only"], discordant)
    return pvalue, pooled, pvalue <= AUDIT_ALPHA


router = APIRouter(prefix="/validator", tags=["validator"])


def _queue_policy_resolver(request: Request) -> QueuePolicySettingsResolver | None:
    """The queue-policy cache, when the app has one bound.

    ``None`` means the app was built without one (some tests construct routers
    directly), in which case callers use the shipped defaults -- the same values
    that were hard-coded before this became operator policy.
    """
    return getattr(request.app.state, "queue_policy_settings", None)


async def _resolve_queue_policy(request: Request) -> QueuePolicySettings:
    resolver = _queue_policy_resolver(request)
    if resolver is None:
        return QUEUE_POLICY_DEFAULTS
    return await resolver.resolve(getattr(request.app.state, "session_maker", None))


async def _deferred_screening_attempts(
    session: AsyncSession, *, agent_ids: Sequence[UUID]
) -> dict[UUID, ScreeningAttempt]:
    """Batch-resolve outstanding mechanical admissions without ledger N+1s.

    A prior deferred ATH lifecycle (pending or resolved) is terminal for
    qualification; an operator clear must not be reopened by a later score
    mutation. A terminal non-build attempt is also a stop even if historical
    data somehow lost its review row.
    """
    ids = tuple(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    reviewed_ids = set(
        await session.scalars(
            select(AthReview.agent_id).where(
                AthReview.agent_id.in_(ids),
                AthReview.algorithm_provenance["review_kind"].as_string()
                == DEFERRED_REVIEW_KIND,
            )
        )
    )
    terminal_ids = set(
        await session.scalars(
            select(ScreeningAttempt.agent_id)
            .where(
                ScreeningAttempt.agent_id.in_(ids),
                ScreeningAttempt.policy_version == SCREENING_POLICY_VERSION,
                ScreeningAttempt.build_only.is_(False),
                ScreeningAttempt.status.in_(("passed", "quarantined")),
            )
            .distinct()
        )
    )
    rows = (
        await session.scalars(
            select(ScreeningAttempt)
            .where(
                ScreeningAttempt.agent_id.in_(ids),
                ScreeningAttempt.policy_version == SCREENING_POLICY_VERSION,
                ScreeningAttempt.build_only.is_(True),
                ScreeningAttempt.status == "passed",
                ScreeningAttempt.reason_code == DEFERRED_MECHANICAL_REASON,
            )
            .order_by(
                ScreeningAttempt.agent_id,
                ScreeningAttempt.finished_at.desc(),
                ScreeningAttempt.attempt_id,
            )
        )
    ).all()
    outstanding: dict[UUID, ScreeningAttempt] = {}
    for attempt in rows:
        if (
            attempt.agent_id in reviewed_ids
            or attempt.agent_id in terminal_ids
            or attempt.agent_id in outstanding
        ):
            continue
        outstanding[attempt.agent_id] = attempt
    return outstanding


async def _deferred_screening_attempt(
    session: AsyncSession, *, agent_id: UUID
) -> ScreeningAttempt | None:
    """Return one admission attempt proving deep review is still outstanding."""
    return (await _deferred_screening_attempts(session, agent_ids=(agent_id,))).get(
        agent_id
    )


async def _record_deferred_review_decision(
    session: AsyncSession,
    *,
    agent: Agent,
    decision: DeferredReviewDecision,
    mode: Literal["observe", "enforce"],
    screening_attempt: ScreeningAttempt | None,
    score_count: int,
    now: datetime,
) -> None:
    """Persist an observe record or enforce an idempotent reward hold."""
    if not decision.triggered:
        return
    retained_review: ScreeningQuarantine | None = None
    if screening_attempt is not None:
        retained_review = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == screening_attempt.attempt_id
            )
        )
    evidence = {
        "sha256": agent.sha256,
        "score_count": score_count,
        "previous_status": agent.status.value,
        "deferred_review": {
            **decision.evidence,
            "mode": mode,
            "screening_attempt_id": (
                str(screening_attempt.attempt_id)
                if screening_attempt is not None
                else None
            ),
            "screening_reason_code": (
                screening_attempt.reason_code if screening_attempt is not None else None
            ),
            "review_audit_digest": (
                retained_review.review_audit_digest
                if retained_review is not None
                else None
            ),
            "review_audit": (
                retained_review.review_audit if retained_review is not None else None
            ),
        },
    }
    existing = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id).with_for_update()
    )
    if mode == "observe":
        # AthReview is unique per agent and may already hold unrelated resolved
        # history. The append-only score audit is therefore the authoritative
        # observe-mode record: every qualification is durable without
        # overwriting, reopening, or being suppressed by that unique row.
        await append_audit_entry(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=None,
            event=EVENT_AUDIT,
            payload={
                "audit_kind": DEFERRED_REVIEW_KIND,
                "enforced": False,
                "qualified": True,
                "trigger_kinds": list(decision.triggers),
            },
            recorded_at=now,
        )
        return

    if existing is not None and existing.status == "pending":
        return

    previous_status = agent.status.value
    if existing is None:
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent.agent_id,
                status="pending",
                opened_at=now,
                original_reason=DEFERRED_REVIEW_REASON,
                original_policy_version=agent.screening_policy_version,
                original_evidence=evidence,
                algorithm_provenance={
                    "snapshot": "score-finalization",
                    "review_kind": DEFERRED_REVIEW_KIND,
                    "algorithm_version": "deferred-source-review-v1",
                    "opened_by": "platform",
                    "backfilled": False,
                    "opened_at_source": "deferred-review-enforce",
                },
            )
        )
    else:
        prior_provenance = existing.algorithm_provenance
        prior_review = {
            "original_reason": existing.original_reason,
            "original_duplicate_of": (
                str(existing.original_duplicate_of)
                if existing.original_duplicate_of is not None
                else None
            ),
            "original_policy_version": existing.original_policy_version,
            "original_evidence": existing.original_evidence,
            "algorithm_provenance": prior_provenance,
            "resolution": existing.resolution,
            "resolution_reason": existing.resolution_reason,
            "resolved_by": existing.resolved_by,
            "resolved_at": (
                existing.resolved_at.isoformat()
                if existing.resolved_at is not None
                else None
            ),
        }
        existing.status = "pending"
        existing.reopened_at = now
        existing.resolved_at = None
        existing.resolved_by = None
        existing.resolution = None
        existing.resolution_reason = None
        existing.original_reason = DEFERRED_REVIEW_REASON
        # The reopened lifecycle is a deferred source review, which has no
        # matched agent. ``agent.duplicate_of`` is cleared below, and
        # ``resolve_copy_review`` refuses to resolve while the two disagree, so
        # a retained copy-hold pointer strands the agent in ATH forever -- the
        # same failure the provenance reset below already guards against.
        existing.original_duplicate_of = None
        existing.original_policy_version = agent.screening_policy_version
        existing.original_evidence = {
            **evidence,
            "prior_review": prior_review,
        }
        # The current pending lifecycle is a deferred review. Claiming the one
        # allowed deep attempt keys off this provenance; retaining a resolved
        # copy/transform kind here would strand the agent in ATH forever.
        existing.algorithm_provenance = {
            "snapshot": "score-finalization",
            "review_kind": DEFERRED_REVIEW_KIND,
            "algorithm_version": "deferred-source-review-v1",
            "opened_by": "platform",
            "backfilled": False,
            "opened_at_source": "deferred-review-reopen",
            "prior_review_kind": prior_provenance.get("review_kind"),
        }
        session.add(
            AthReviewAction(
                action_id=uuid4(),
                review_id=existing.review_id,
                action="reopen",
                reason=DEFERRED_REVIEW_REASON,
                actor="platform:deferred-source-review",
                evidence={
                    "sha256": agent.sha256,
                    "score_count": score_count,
                    "previous_status": previous_status,
                },
                created_at=now,
            )
        )
    agent.status = AgentStatus.ATH_PENDING_REVIEW
    agent.duplicate_of = None
    agent.review_reason = DEFERRED_REVIEW_REASON


async def _evaluate_and_record_deferred_review(
    session: AsyncSession,
    *,
    agent: Agent,
    bench_version: int,
    score_count: int,
    settings: DeferredSourceReviewSettings,
    now: datetime,
) -> None:
    """Re-evaluate deferred admissions after a canonical ledger mutation.

    This helper is used both at first quorum and on operator-authorized score
    replacement. The explicit flush is load-bearing: ``list_eligible_ledger``
    filters on the agent's finalized status and must see the just-written score
    and status before deciding rank/anomaly eligibility. In enforce mode a
    mutation can promote a *different* row into the top five, so the whole
    canonical same-version ledger is reconsidered. Only rows carrying the
    immutable mechanical-admission marker can acquire a delayed hold, and the
    marker resolver suppresses pending or terminal deep-review lifecycles.
    """
    # ``off`` and ``bypass`` differ only in pre-score depth (see
    # ``claim_screening_attempts``); neither computes anything after scoring, so
    # the canonical ledger is never even read for them.
    if (
        settings.mode == "off"
        or settings.mode == "bypass"
        or agent.status
        not in {
            AgentStatus.SCORED,
            AgentStatus.LIVE,
        }
    ):
        return
    mode: Literal["observe", "enforce"] = settings.mode
    await session.flush()
    ledger = await list_eligible_ledger(
        session,
        bench_version=bench_version,
        include_fingerprints=False,
        include_details=False,
    )
    if mode == "observe":
        decision = evaluate_deferred_review(
            agent_id=agent.agent_id,
            ledger=ledger,
            settings=settings,
        )
        await _record_deferred_review_decision(
            session,
            agent=agent,
            decision=decision,
            mode=mode,
            screening_attempt=None,
            score_count=score_count,
            now=now,
        )
        return

    ledger_ids = [row.agent_id for row in ledger if row.eligible]
    if not ledger_ids:
        return
    candidates = {
        candidate.agent_id: candidate
        for candidate in await session.scalars(
            select(Agent).where(
                Agent.agent_id.in_(ledger_ids),
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
            )
        )
    }
    score_counts = {
        candidate_id: int(count)
        for candidate_id, count in (
            await session.execute(
                select(Score.agent_id, func.count())
                .where(
                    Score.agent_id.in_(ledger_ids),
                    Score.bench_version == bench_version,
                )
                .group_by(Score.agent_id)
            )
        ).all()
    }
    admissions = await _deferred_screening_attempts(
        session, agent_ids=tuple(candidates)
    )
    for row in ledger:
        candidate = candidates.get(row.agent_id)
        if candidate is None or not row.eligible:
            continue
        admission_attempt = admissions.get(row.agent_id)
        if admission_attempt is None:
            # Legacy/full-reviewed rows never acquire a new hold merely because
            # the control is enabled later. Resolved terminal deep reviews are
            # likewise suppressed by the marker resolver.
            continue
        decision = evaluate_deferred_review(
            agent_id=row.agent_id,
            ledger=ledger,
            settings=settings,
        )
        await _record_deferred_review_decision(
            session,
            agent=candidate,
            decision=decision,
            mode="enforce",
            screening_attempt=admission_attempt,
            score_count=score_counts.get(row.agent_id, 0),
            now=now,
        )


async def _evaluate_and_record_outlier_escalation(
    session: AsyncSession,
    *,
    agent: Agent,
    bench_version: int,
    composite: float,
    cohort: Sequence[float],
    settings: OutlierEscalationSettings,
    now: datetime,
) -> None:
    """Escalate an out-of-band composite to ATH review (issue #476).

    The safety net for a gamed fresh contract: on the score-finalization
    transition, a composite that is a robust upward outlier of its comparable
    same-benchmark cohort is routed to ``ATH_PENDING_REVIEW`` for operator
    adjudication instead of being ranked. This reuses the exact hold mechanism
    the copy and transform-audit holds use -- ``agents.status`` +
    ``agents.review_reason`` + a pending ``ath_reviews`` row carrying the
    immutable evidence snapshot -- so the operator queue, resolution flow and
    ledger exclusion all work unchanged.

    Auto-HOLD, never auto-reject. ``mode="observe"`` records a would-be hold on
    the append-only audit chain without touching status; ``mode="enforce"``
    opens the hold. Bench-version scoped so v8-v11 are untouched, and gated on
    ``SCORED`` so a copy / transform-audit hold that already fired this
    transition wins outright (this path never re-holds an already-held agent).
    """
    if settings.mode == "off":
        return
    if bench_version < settings.min_bench_version:
        return
    # A copy or transform-audit hold that already fired moved the agent out of
    # SCORED; the deferred path acts only on SCORED/LIVE for the same reason.
    if agent.status != AgentStatus.SCORED:
        return

    decision = evaluate_score_outlier(
        composite=composite, cohort=cohort, settings=settings
    )
    if not decision.held:
        # In-band, below the floor, or an insufficient cohort. Rank normally and
        # leave no hold; the "why not" lives in the decision evidence, which is
        # only persisted when a would-be hold fires (below), matching the other
        # holds' record-on-trigger discipline.
        return

    if settings.mode == "observe":
        # Record the would-be hold on the durable audit chain without holding,
        # so a later switch to enforce leaves no gap in the evidence. AthReview
        # is unique-per-agent and may carry unrelated resolved history, so the
        # append-only chain -- not that row -- is the authoritative observe log.
        await append_audit_entry(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=None,
            event=EVENT_AUDIT,
            payload={
                "audit_kind": OUTLIER_REVIEW_KIND,
                "enforced": False,
                "qualified": True,
                "bench_version": bench_version,
                "evidence": decision.evidence,
            },
            recorded_at=now,
        )
        logger.info(
            "agent %s: out-of-band composite would hold (observe mode): %s",
            agent.agent_id,
            decision.reason,
        )
        return

    # enforce -- open the hold. Idempotency: the unique (agent_id) row means a
    # pending review already covering this agent must not be duplicated. A fresh
    # first-quorum finalization has none; the guard protects any re-entry.
    existing = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id).with_for_update()
    )
    if existing is not None:
        return

    agent.status = AgentStatus.ATH_PENDING_REVIEW
    agent.review_reason = decision.reason
    session.add(
        AthReview(
            review_id=uuid4(),
            agent_id=agent.agent_id,
            status="pending",
            opened_at=now,
            original_reason=decision.reason,
            original_policy_version=agent.screening_policy_version,
            original_evidence=decision.evidence,
            algorithm_provenance={
                "snapshot": "score-finalization",
                "review_kind": OUTLIER_REVIEW_KIND,
                "algorithm_version": OUTLIER_ALGORITHM_VERSION,
                "opened_by": "platform",
                "backfilled": False,
                "opened_at_source": "outlier_escalation",
            },
        )
    )
    await append_audit_entry(
        session,
        agent_id=agent.agent_id,
        validator_hotkey=None,
        event=EVENT_AUDIT,
        payload={
            "audit_kind": OUTLIER_REVIEW_KIND,
            "enforced": True,
            "qualified": True,
            "bench_version": bench_version,
            "evidence": decision.evidence,
        },
        recorded_at=now,
    )
    logger.warning(
        "agent %s held for out-of-band score review: %s",
        agent.agent_id,
        decision.reason,
    )


async def _fresh_submission_lane_due(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    bench_version: int,
    rollout_started_at: datetime,
    now: datetime,
    settings: QueuePolicySettings,
) -> bool:
    """Whether this validator's next rollout-era job serves a fresh submission.

    The lane split defaults to three fresh-submission jobs for every one
    rollout-tail job. The counter is per validator, so every validator rotates
    through both lanes and new agents can still reach the three-validator
    scoring quorum.

    Live issued tickets reserve positions as soon as they are leased. Without
    that reservation, every concurrent worker slot observes the same completed
    count and the whole validator bursts into the fresh lane before any one job
    can finish. PostgreSQL serializes this count-and-lease decision per
    validator and rollout; the transaction keeps the lock until the selected
    ticket is written. Expired leases do not reserve a position forever.

    The split is operator policy
    (``ditto.api_models.queue_policy_settings.QueuePolicySettings``), but the
    modulus is deliberately immutable while a rollout is open: this count is
    measured from ``rollout_started_at``, so changing the cycle length would
    reassign every validator's position in it discontinuously. The admin
    endpoint refuses such a write rather than letting it land here.
    """
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        (
                            "fresh-submission-lane:"
                            f"{validator_hotkey}:{bench_version}:"
                            f"{rollout_started_at.isoformat()}"
                        ),
                        0,
                    )
                )
            )
        )
    reserved_since_rollout = await session.scalar(
        select(func.count())
        .select_from(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.created_at >= rollout_started_at,
            or_(
                ValidatorTicket.status == TicketStatus.SCORED,
                (
                    (ValidatorTicket.status == TicketStatus.ISSUED)
                    & (ValidatorTicket.deadline > now)
                ),
            ),
        )
    )
    return settings.fresh_submission_lane_due(int(reserved_since_rollout or 0))


# Unparseable slot ids sort above every cap, so an unrecognised id is declined
# rather than silently treated as slot zero.
_UNRANKED_SLOT = 1 << 16


def _slot_ordinal(slot_id: str) -> int:
    """Return the ordinal N of a ``slot-N`` id, or a value above every cap.

    Used only to reject ids outside the wire contract's ``^slot-[0-7]$``.
    Anything that does not parse fails closed. The ordinal is deliberately NOT
    what the operator cap compares against -- see :func:`_slot_cap_declines`.
    """
    prefix, _, suffix = slot_id.partition("-")
    if prefix != "slot" or not suffix.isdigit():
        return _UNRANKED_SLOT
    return int(suffix)


async def _held_lease_slots(
    session: AsyncSession, *, validator_hotkey: str, now: datetime
) -> set[str]:
    """Slot ids on which this validator currently holds a live lease.

    ``deadline > now`` rather than status alone, because the overdue sweep runs
    downstream of the cap gate: an expired-but-unswept lease is not occupied
    capacity and must not be charged against the cap.
    """
    return set(
        (
            await session.scalars(
                select(ValidatorTicket.slot_id)
                .where(
                    ValidatorTicket.validator_hotkey == validator_hotkey,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.deadline > now,
                )
                .distinct()
            )
        ).all()
    )


def _inference_stage_slot_cap(config: InferenceProxyConfig) -> int:
    """Maximum concurrent post-v7 leases one validator may seed safely.

    Every live benchmark can occupy one per-ticket embedding lane.  Leasing
    more benchmarks than the validator-wide lane can admit turns ordinary
    backpressure into synchronized multi-minute startup failures.  Keep one
    slot available even under a malformed/disabled emergency setting; the
    inference readiness gates still decide whether that slot is leaseable.
    """
    per_ticket = max(1, config.embedding_per_ticket_concurrency)
    return max(
        1,
        min(
            HARD_SLOT_CEILING,
            config.embedding_per_validator_concurrency // per_ticket,
        ),
    )


_INFERENCE_STARTUP_STAGES = {
    "preparing",
    "building_harness",
    "generating_dataset",
    "starting_harness",
    "waiting_for_relay",
}


def _inference_stage_cap_declines(
    *,
    slot_id: str,
    slot_running_benchmark: bool,
    allowed_slots: int,
    capacity: BenchmarkCapacity,
) -> bool:
    """Whether another post-v7 startup would overfill hosted embeddings.

    Only startup stages occupy the embedding rail heavily.  A benchmark that
    has reached ``running_benchmark`` should not cost the validator a startup
    slot for the rest of its run; doing so would turn a safety rail into idle
    capacity.  Protocol-16 active slots with no progress are conservatively
    startup work because they are pulling, rendering, or seeding.
    """
    if slot_running_benchmark:
        return False
    active_startups = sum(
        1
        for slot in capacity.active
        if slot.slot_id != slot_id
        and slot.bench_version >= 7
        and (slot.progress is None or slot.progress.stage in _INFERENCE_STARTUP_STAGES)
    )
    return active_startups >= allowed_slots


def _slot_cap_declines(
    *,
    slot_id: str,
    slot_running_benchmark: bool,
    allowed_slots: int,
    held_slots: Collection[str],
) -> bool:
    """Whether the operator slot cap refuses a NEW lease on ``slot_id``.

    The cap counts concurrent leases, not slot ordinals. #433 gated on the
    ordinal instead, on the stated assumption that "the validator numbers its
    slots densely from zero". Production disproves it: ``healthy_slots`` is a
    sparse subset of ``configured_slots`` whenever a slot is draining or
    unhealthy, so a validator advertising four slots with slot-0 unhealthy has
    only ordinals 1-3 to offer and an ordinal ceiling of three silently costs
    it the third lease the operator granted. Counting what the validator
    actually holds is also what :func:`allowed_slot_count` already documents
    its result is for.

    ``slot_id`` is excluded from the count: a slot polling for its own live
    lease is served by the resume path downstream, and charging that lease
    against the cap would make a validator at exactly the cap unable to pick
    its own work back up after a restart.

    Live work is still exempt (a slot the heartbeat reports as running), so
    lowering the cap continues to cost the fleet new work only, never leases
    already in flight.
    """
    if slot_running_benchmark:
        return False
    if _slot_ordinal(slot_id) >= HARD_SLOT_CEILING:
        return True
    return len({held for held in held_slots if held != slot_id}) >= allowed_slots


def _slot_cap_decline_reason(
    *,
    slot_id: str,
    settings: ValidatorSlotSettings,
    advertised_slots: int,
    sample: HostResourceSample | None = None,
    disk_percent: int | None = None,
) -> DispatchDeclineReason:
    """Name the lever behind a :func:`_slot_cap_declines` refusal.

    Observability only -- read *after* the decision, never as part of it, so
    that a wrong label can never cost a validator a lease.

    :func:`allowed_slot_count` folds three separate operator levers into one
    integer, and an operator staring at an idle validator cannot tell which one
    is holding it back. The three answers want different actions: a
    ``slot_ceiling`` id is a validator sending something outside the
    ``^slot-[0-7]$`` wire contract (a bug on their side, not a policy), a
    ``disk_breaker`` clears itself once the host frees disk, and a ``slot_cap``
    is a backroom setting somebody has to raise.

    The breaker is only named when it actually *narrowed* the allowance: with a
    ``max_concurrent_slots`` already at or below the resource throttle's
    one-slot allowance, the operator cap alone would have declined this poll.
    The wire label retains its historical ``disk_breaker`` name while the
    breaker now covers CPU, memory, and disk pressure.
    """
    if _slot_ordinal(slot_id) >= HARD_SLOT_CEILING:
        return "slot_ceiling"
    observed = sample or HostResourceSample(disk_percent=disk_percent)
    unrestricted = allowed_slot_count(
        settings,
        advertised_slots=advertised_slots,
        sample=HostResourceSample(),
    )
    if (
        allowed_slot_count(
            settings,
            advertised_slots=advertised_slots,
            sample=observed,
        )
        < unrestricted
    ):
        return "disk_breaker"
    return "slot_cap"


def _record_dispatch_decline(
    reason: DispatchDeclineReason, *, validator_hotkey: str, slot_id: str
) -> None:
    """Count and log a validator job poll that is about to be answered 204.

    Every ``return Response(status_code=204)`` on the dispatch path pairs with a
    call to this, so the counter's reasons partition the declines rather than
    sampling them: a 204 that no reason explains is a missing call site, and the
    sum across reasons is the total decline rate.

    The hotkey and slot stay off the counter (per-validator series would make
    it unusable at fleet size) and go on the log line, which is where an
    operator lands once the metric has told them *which* gate to go read about.
    """
    VALIDATOR_DISPATCH_DECLINED.labels(reason=reason).inc()
    logger.info(
        "declined job reason=%s validator=%s slot=%s",
        reason,
        validator_hotkey,
        slot_id,
    )


async def _idle_retest_slot(
    session: AsyncSession,
    *,
    heartbeat: ValidatorHeartbeat | None,
    slot_settings: ValidatorSlotSettings,
    validator_hotkey: str,
    now: datetime,
    requested_slot_id: str | None = None,
) -> str | None:
    """Pick a slot a continual retest may occupy, or None when there is none.

    The continual lane consumes an execution slot just like a canonical lease,
    so it is bound by the same two limits: the validator must be offering the
    slot (``healthy_slots`` minus whatever it reports as already running), and
    the operator's ``max_concurrent_slots`` ceiling on how many leases one
    validator holds at once. Before this the lane asked neither -- every
    confirmation ticket took the ``slot-0`` column default and none of them
    counted against the cap, so an operator lowering the cap did not bound
    retests and a retest could not be told apart from an idle slot.

    Slots holding a live lease are excluded on top of the heartbeat's own
    ``active`` list: the heartbeat is a self-report that freezes when ingest
    fails, while the ticket table is what the platform actually leased. Taking
    the union keeps a stale capacity blob from double-booking a slot, which the
    unique partial index would otherwise reject outright.

    A validator advertising no parseable capacity is treated as the single
    ``slot-0`` machine this lane has always assumed, not as having no capacity
    at all. Absence of evidence is not evidence of a busy slot, and declining
    would silently switch the lane off for anyone whose heartbeat predates -- or
    momentarily loses -- the capacity blob. The per-slot rail in
    ``issue_confirmation_ticket`` still refuses the claim if that one slot turns
    out to be leased.

    Returns the lowest free healthy slot for determinism -- the caller's
    fairness ordering is over cohort members, not slots, so there is nothing to
    gain by spreading and a stable choice is easier to reason about.
    """
    if heartbeat is None:
        return None
    try:
        capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
    except ValidationError:
        capacity = None
    held = await _held_lease_slots(session, validator_hotkey=validator_hotkey, now=now)
    allowed = allowed_slot_count(
        slot_settings,
        advertised_slots=(capacity.configured_slots if capacity is not None else 1),
        sample=_heartbeat_resource_sample(heartbeat),
    )
    if len(held) >= allowed:
        return None
    offered = capacity.free_healthy_slots if capacity is not None else ("slot-0",)
    free = [slot for slot in offered if slot not in held]
    if not free:
        return None
    if requested_slot_id is not None:
        return requested_slot_id if requested_slot_id in free else None
    return min(free, key=_slot_ordinal)


def _heartbeat_percent(metrics: object, key: str) -> int | None:
    """Read one coarse percentage out of the stored metrics blob."""
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _heartbeat_resource_sample(
    heartbeat: ValidatorHeartbeat | None,
) -> HostResourceSample:
    """Read the last reported host utilisation, per resource.

    Unknown is deliberately not treated as a tripped breaker: a validator that
    reports no metrics must not lose the slots it already had.
    """
    if heartbeat is None:
        return HostResourceSample()
    metrics = heartbeat.system_metrics
    return HostResourceSample(
        cpu_percent=_heartbeat_percent(metrics, "cpu_percent"),
        memory_percent=_heartbeat_percent(metrics, "memory_percent"),
        disk_percent=_heartbeat_percent(metrics, "disk_percent"),
    )


async def _validator_slot_settings(request: Request) -> ValidatorSlotSettings:
    """Resolve the operator slot cap, falling back to the conservative default.

    A missing resolver (an app built without lifespan) must not uncap the
    fleet, so the default policy is returned instead of an unbounded one. The
    resolution itself is shared with the public fleet view, which must report
    the same cap this path enforces.
    """
    return await resolve_slot_settings(request.app.state)


PREV_GEN_CARRYOVER_DEFAULTS = QUEUE_POLICY_DEFAULTS.prev_gen_carryover
"""Previous-generation policy in force when a caller supplies none.

The shipped defaults, so an omitted policy makes retired-era work stricter --
never looser -- than an operator's stored revision would.
"""


async def _desired_era_capable_hotkeys(
    session: AsyncSession, *, rollout: BenchmarkRollout, now: datetime
) -> set[str]:
    """Validators whose fresh signed heartbeat advertises the desired version.

    The fleet that could actually take desired-era work. A stale or
    source-version-only heartbeat is not part of it, so a validator that cannot
    serve the new era never makes the previous generation look like it is
    crowding anything.
    """
    heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
    return {
        candidate.validator_hotkey
        for candidate in heartbeats
        if heartbeat_supports_version(
            candidate, now=now, version=rollout.desired_version
        )
    }


async def _prev_gen_lane_open(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    now: datetime,
    settings: PrevGenCarryoverSettings,
) -> bool:
    """Whether previous-generation work may issue at all on this poll.

    Strict priority, shared by both previous-generation lanes: reaching the tail
    of ``request_job`` only proves that *this* validator found no desired-era
    work, and that happens constantly while the queue is deep (one ticket per
    agent/version/validator, one generation per owner, per-validator cooldowns).
    Leasing retired-era work on that evidence takes a slot away from a queue the
    validator simply could not see.

    See :mod:`ditto.db.queries.desired_era_backlog` for why the fleet-wide
    answer errs toward "still outstanding" and why it nonetheless terminates.
    """
    if not settings.require_desired_era_drained:
        return True
    return not await desired_era_work_outstanding(
        session,
        rollout=rollout,
        now=now,
        capable_validator_hotkeys=await _desired_era_capable_hotkeys(
            session, rollout=rollout, now=now
        ),
    )


async def _issue_source_backfill_ticket(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    heartbeat: ValidatorHeartbeat | None,
    validator_hotkey: str,
    now: datetime,
    active_version: int,
    artifact_mode: Literal["legacy", "prefer_screened", "screened_only"],
    validator_running_benchmark: bool,
    slot_id: str,
    efficiency_config: EfficiencyBonusConfig | None = None,
    # Defaults to the conservative policy so an omitted cap narrows the
    # backfill budget rather than widening it.
    slot_settings: ValidatorSlotSettings = SLOT_SETTINGS_DEFAULT,
    carryover_settings: PrevGenCarryoverSettings = PREV_GEN_CARRYOVER_DEFAULTS,
    owner_concurrent_submission_limit: int = OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    similarity_policy: SimilarityBudgetPolicy | None = None,
    similarity_concurrent_submission_limit: int = (
        SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT
    ),
    resume_only: bool = False,
) -> ValidatorTicket | None:
    """Use otherwise-idle capacity after the desired era has nothing to give.

    Previous-generation work answers to the same operator policy as the adopted
    carryover below (``queue_policy.prev_gen_carryover``): by default it waits
    for the cohort and a genuinely empty desired-era queue, while an operator
    can explicitly relax either gate for bounded interleaving.

    ``active_version`` is the era the fleet is actually scoring. While a rollout
    is open it equals ``rollout.from_version``, and this lane does what it was
    written for (#362): keep a source-version validator busy draining the source
    backlog instead of idling through the transition, producing scores that
    still count. That is the ONLY case this lane serves now -- see the floor
    below.
    """
    # Both previous-generation lanes answer to the same master switch. The
    # adopted-carryover helper already enforced it, but source-era backfill did
    # not, so `enabled=false` still issued ordinary v8 work during the v9
    # rollout. Check before resumption and new admission alike: an operator who
    # turns this policy off is closing the whole previous-generation lane.
    if not carryover_settings.enabled:
        return None
    # The floor, checked before anything else and before the resume path.
    #
    # Two separate conditions, because they fail for different reasons and one
    # does not imply the other:
    #
    # * ``from_version < active_version`` -- the rollout activated and this
    #   era is behind the fleet. Every ticket would be for a version no quorum
    #   will ever be assembled on.
    # * ``from_version < MIN_SCOREABLE_BENCH_VERSION`` -- the era is RETIRED
    #   outright. Its scores cannot be written at all; the database rejects
    #   them and the ticket trigger rejects the lease.
    #
    # This sits ABOVE the existing-lease resume below, and that placement is the
    # entire point. The resume path re-issued an unexpired ``from_version``
    # lease before reaching the old retired-era gate, and ``request_job``
    # resurrects the activated v7 rollout to feed this lane -- whose
    # ``from_version`` is 6. So "no rollout is open" did not mean "no v6
    # tickets": a v6 lease kept renewing itself indefinitely with no flag
    # involved. Resumption is not exempt from a floor. A lease for an era that
    # can no longer be scored is not work in progress, it is a slot the live era
    # does not get, and the score at the end of it would be rejected anyway.
    if (
        rollout.from_version < active_version
        or rollout.from_version < MIN_SCOREABLE_BENCH_VERSION
    ):
        return None
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=rollout.from_version
    ):
        return None
    if carryover_settings.require_cohort_complete and not await rollout_cohort_complete(
        session, rollout=rollout, cohort_size=rollout.cohort_size
    ):
        return None
    is_postgresql = session.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        # Keep the same first lock as issue_ticket. Reacquiring it below is
        # transaction-local and safe. The row lock after it makes the
        # resume-vs-new decision stable against concurrent score submission.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{validator_hotkey}:{slot_id}", 0)
                )
            )
        )
    existing = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.bench_version == rollout.from_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .with_for_update()
    )
    if existing is not None:
        return await issue_ticket(
            session,
            validator_hotkey=validator_hotkey,
            now=now,
            ttl=_TICKET_TTL,
            bench_version=rollout.from_version,
            artifact_mode=artifact_mode,
            validator_running_benchmark=validator_running_benchmark,
            slot_id=slot_id,
            efficiency_config=efficiency_config,
            owner_concurrent_submission_limit=owner_concurrent_submission_limit,
            similarity_policy=similarity_policy,
            similarity_concurrent_submission_limit=(
                similarity_concurrent_submission_limit
            ),
        )
    if resume_only:
        return None
    # NEW admission below. The retired-era question is already settled at the
    # top of this function, for resume and admission alike, and there is no
    # longer a setting that can re-open it.
    #
    # `_prev_gen_lane_open` never could have stood in for that floor. It asks
    # whether any desired-era work is leasable *right now*, and "nothing
    # leasable this instant" is not "the desired era is finished": owner
    # serialization, the per-(agent, version, validator) rule and quorum-sized
    # capable fleets all make a deep v7 queue momentarily unleasable, at which
    # point that gate correctly reports drained. Priority was the wrong axis --
    # a retired era does not need to be last, it needs to be never.
    #
    # Checked before the fleet lock so a poll that is going to decline anyway
    # does not serialize behind one that is not.
    if not await _prev_gen_lane_open(
        session, rollout=rollout, now=now, settings=carryover_settings
    ):
        return None
    if is_postgresql:
        acquired_fleet_lock = await session.scalar(
            select(
                func.pg_try_advisory_xact_lock(
                    func.hashtextextended(f"source-backfill:{rollout.rollout_id}", 0)
                )
            )
        )
        if not acquired_fleet_lock:
            # A desired-version allocation earlier in this transaction may
            # retain a per-owner lock. Never wait here while a source allocator
            # holds the fleet lock and waits for that owner; yield this poll and
            # let the validator retry instead of creating a lock-order cycle.
            return None
    # The fleet lock above serializes this new-admission decision with the
    # ticket write. Capacity=1 remains useful for draining v6, while a
    # multi-validator v7 fleet keeps at least one desired-version slot free.
    desired_slots = 0
    heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
    for candidate in heartbeats:
        supports_source = heartbeat_supports_version(
            candidate, now=now, version=rollout.from_version
        )
        supports_desired = heartbeat_supports_version(
            candidate, now=now, version=rollout.desired_version
        )
        if not supports_source and not supports_desired:
            continue
        if candidate.protocol_version >= 10:
            try:
                capacity = BenchmarkCapacity.model_validate(
                    candidate.benchmark_capacity
                )
            except ValidationError:
                continue
            # Count the slots the platform will actually fill, not the ones the
            # host merely advertises. Validators advertise headroom so the cap
            # can be raised without a release; counting that headroom here would
            # widen the source-backfill budget to capacity that never receives a
            # ticket, and drown the desired version this reservation protects.
            slot_count = (
                allowed_slot_count(
                    slot_settings,
                    advertised_slots=len(capacity.healthy_slots),
                    sample=_heartbeat_resource_sample(candidate),
                )
                if capacity.admission == "accepting"
                else 0
            )
        else:
            slot_count = 1
        if supports_desired:
            desired_slots += slot_count
    max_active_backfills = max(1, desired_slots - 1)
    active_backfills = int(
        await session.scalar(
            select(func.count())
            .select_from(ValidatorTicket)
            .where(
                ValidatorTicket.bench_version == rollout.from_version,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
        )
        or 0
    )
    if active_backfills >= max_active_backfills:
        return None
    return await issue_ticket(
        session,
        validator_hotkey=validator_hotkey,
        now=now,
        ttl=_TICKET_TTL,
        bench_version=rollout.from_version,
        artifact_mode=artifact_mode,
        validator_running_benchmark=validator_running_benchmark,
        slot_id=slot_id,
        efficiency_config=efficiency_config,
        owner_concurrent_submission_limit=owner_concurrent_submission_limit,
        similarity_policy=similarity_policy,
        similarity_concurrent_submission_limit=(similarity_concurrent_submission_limit),
    )


async def _issue_prev_gen_carryover_ticket(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    heartbeat: ValidatorHeartbeat | None,
    validator_hotkey: str,
    now: datetime,
    settings: PrevGenCarryoverSettings,
    target_inference_ready: bool,
    validator_running_benchmark: bool,
    slot_id: str,
    efficiency_config: EfficiencyBonusConfig | None = None,
    owner_concurrent_submission_limit: int = OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    similarity_policy: SimilarityBudgetPolicy | None = None,
    similarity_concurrent_submission_limit: int = (
        SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT
    ),
) -> ValidatorTicket | None:
    """Lease an adopted previous-generation submission in the new era.

    The third leg of the carryover contract. Admission (the carryover row) and
    generation (the desired-version dataset) are both already in place before
    this can find anything, but neither is enough on its own: every existing
    desired-version issuance path in this endpoint passes
    ``submitted_at_or_after=rollout.created_at``, which filters on
    ``Agent.created_at``, so a fully admitted and fully datasetted
    previous-generation agent is leased by NO other path. ``only_agent_ids``
    replaces that arrival filter with the explicit adopted set.

    Deliberately confined to the **cohort lane**. The fresh-submission lane
    exists to keep new miners from starving behind a transition, and diluting it
    with a backlog drain would undo that. Under strict priority the caller
    reaches here only after the desired-era lanes have nothing to give. When an
    operator explicitly sets ``require_desired_era_drained=False``, it reaches
    here first on a cohort-lane poll so bounded carryover can actually
    interleave instead of starving behind a continuously ready queue.

    By default it also waits for the inherited cohort to settle, on the same
    precedent as :func:`_issue_source_backfill_ticket` ("use otherwise-idle
    capacity after the inherited cohort settles"): carryover rides on a
    transition and must never be able to delay the one it is riding on.

    Lane position is necessary but not sufficient, so it additionally waits for
    the desired-era queue to be fleet-wide empty -- see :func:`_prev_gen_lane_open`.
    """
    if not settings.enabled or not target_inference_ready:
        return None
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=rollout.desired_version
    ):
        return None
    if settings.require_cohort_complete and not await rollout_cohort_complete(
        session, rollout=rollout, cohort_size=rollout.cohort_size
    ):
        return None
    if not await _prev_gen_lane_open(
        session, rollout=rollout, now=now, settings=settings
    ):
        return None
    adopted = await carryover_agent_ids(session, rollout=rollout)
    if not adopted:
        return None
    return await issue_ticket(
        session,
        validator_hotkey=validator_hotkey,
        now=now,
        ttl=_TICKET_TTL,
        bench_version=rollout.desired_version,
        artifact_mode="screened_only",
        validator_running_benchmark=validator_running_benchmark,
        slot_id=slot_id,
        only_agent_ids=adopted,
        efficiency_config=efficiency_config,
        owner_concurrent_submission_limit=owner_concurrent_submission_limit,
        similarity_policy=similarity_policy,
        similarity_concurrent_submission_limit=(similarity_concurrent_submission_limit),
    )


def _prev_gen_carryover_precedes_desired_era(
    *, fresh_lane_due: bool, settings: PrevGenCarryoverSettings
) -> bool:
    """Whether this cohort-lane poll may interleave adopted carryover first.

    ``require_desired_era_drained=False`` is the operator's explicit choice to
    give adopted work bounded capacity before the desired-era queue empties.
    The request path must therefore consult carryover before its normal cohort
    and fresh fallbacks on a non-fresh slot; consulting it last makes the knob
    ineffective whenever ordinary desired-era work stays continuously ready.

    Fresh-lane slots remain reserved for new submissions regardless of this
    setting.  The carryover helper still enforces capability, cohort-completion,
    admission, owner, and similarity gates before it can issue anything.
    """
    return (
        not fresh_lane_due
        and settings.enabled
        and not settings.require_desired_era_drained
    )


# How long a pre-signed artifact URL stays valid.
_ARTIFACT_URL_TTL = timedelta(minutes=5)

# How long a validator has to redeem a ticket with a score before it lapses and
# the slot re-opens for another validator.
# Keep the lease longer than the validator's 110-minute benchmark cap.
# The remaining ten minutes cover artifact/setup time and the validator's
# explicit two-minute signed-report margin.
# A productive full Bench 9 run can legitimately need roughly 100 minutes.
# The validator retains a 15-minute unchanged-progress watchdog and a separate
# reporting margin, so a two-hour lease funds slow progress without turning a
# wedged scorer into a two-hour silent expiry.
_TICKET_TTL = timedelta(minutes=120)

# Signed job claims outside this window are stale. A consumed nonce remains in
# the database for the same window, making replay rejection consistent across
# every API replica without introducing another secret.
_JOB_REQUEST_MAX_AGE = timedelta(minutes=2)
# A leased seed of ``None`` is meaningful (a legacy bundle lease), so "this
# validator holds no lease" needs a sentinel distinct from it.
_MISSING_LEASE: Any = object()
# Throttle + timeout for the post-commit on-chain weight-confirmation sweep that
# arms a king's public source-release window. Bounds how often the score path
# reads the revealed weight matrix while any king still awaits confirmation.
_KING_WEIGHT_CHECK_INTERVAL = timedelta(minutes=5)
_KING_WEIGHT_CHECK_TIMEOUT_SECONDS = 5.0
_QUALIFICATION_REFRESH_INTERVAL_SECONDS = 30.0
_qualification_refresh_due = 0.0

# Reject captured heartbeats outside a short clock-skew/retry window. Workers
# report every two minutes, so five minutes tolerates normal transient outages.
_HEARTBEAT_MAX_SKEW_SECONDS = 300
_HEARTBEAT_MAX_BYTES = 16 * 1024


# Object-store key the upload pipeline writes the tarball under.
def _artifact_key(agent_id: UUID) -> str:
    return f"{agent_id}/agent.tar.gz"


def _screened_image_key(agent_id: UUID, image_upload_id: UUID) -> str:
    """Return the immutable accepted screener image object key."""
    return f"{agent_id}/screened-images/{image_upload_id}.tar"


# Agents the validator may pull as work. The partial index covers exactly
# Agents a score may be reported against. ``scored`` / ``live`` are included
# so a validator can re-score across epochs without a 409;
# ``ath_pending_review`` is included so a re-score of a held agent updates its
# score row (feeding the eventual review) without un-holding it.
_SCOREABLE_STATUSES = SCOREABLE_AGENT_STATUSES


async def _refresh_qualification_if_due(
    session: AsyncSession,
    *,
    generator: DatasetGenerator,
    now: datetime,
    inference_config: InferenceProxyConfig | None = None,
) -> None:
    """Single-flight best-effort convergence for authenticated idle pollers."""
    global _qualification_refresh_due
    monotonic_now = time.monotonic()
    if monotonic_now < _qualification_refresh_due:
        return
    # Set before the first await so concurrent requests in this process collapse
    # into one refresh. Score/verdict triggers remain the immediate primary path.
    _qualification_refresh_due = monotonic_now + _QUALIFICATION_REFRESH_INTERVAL_SECONDS
    try:
        if inference_config is None:
            await refresh_rolling_qualification(session, generator=generator, now=now)
        else:
            await refresh_rolling_qualification(
                session,
                generator=generator,
                now=now,
                inference_config=inference_config,
            )
    except Exception:
        logger.exception("automatic benchmark qualification refresh failed")


class ValidatorAuthError(Exception):
    """Raised when a validator request fails authentication/authorization.

    Covers a missing/malformed ``X-Validator-Hotkey`` header, a hotkey not
    registered on the netuid, a hotkey without a ``validator_permit``, and
    a score whose signature does not verify. The envelope handler maps all
    of these to HTTP 401 + code 4000.
    """


class RetiredBenchVersionError(Exception):
    """Raised when a score is submitted for a benchmark era that is retired.

    Maps to HTTP **410 Gone** + code 4002, and the status is the whole point.
    This is TERMINAL and it is the validator's cue to stop: the era is gone,
    the database will not accept the row, and no amount of retrying changes
    that. 409 was available and deliberately not used -- it reads as a
    conflict, and a conflict invites a retry.

    It must never be reported back as ``fail_job(reason="infrastructure")``.
    Infrastructure is NO-FAULT: it mints a compensating grant, raises the
    attempt cap and re-leases, forever. That is the exact loop that burned
    4.5 validator-hours per attempt on the ``mnemo*`` family, and a retired
    era would feed it indefinitely because the condition never clears. The
    correct hand-back is ``scoring_error`` (consumes the attempt, no grant),
    which is what the canonical lane already does for a ``PlatformError`` out
    of ``submit_score``.

    Belt and braces, though: even a misclassified ``infrastructure`` report
    cannot loop, because the reissue it asks for has to insert a sub-v7
    ``validator_tickets`` row and the
    ``validator_tickets_bench_version_floor`` trigger refuses it. The lease
    dies either way. That is the difference between closing this in policy and
    closing it in the schema.
    """


class AgentNotEvaluatableError(Exception):
    """Raised when a score is submitted for an agent not in a scoreable state.

    A score is only accepted once an agent has reached evaluation
    (``evaluating`` / ``scored`` / ``live``). Reporting against an
    ``uploaded`` / ``screening*`` / ``banned`` agent is a no-op the daemon
    should not retry, so it maps to HTTP 409 (code 4001).
    """


ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
GeneratorDep = Annotated[DatasetGenerator, Depends(get_dataset_generator)]


def _dev_bypass_permit(network: str) -> bool:
    """Whether the dev "skip the validator permit check" escape hatch is active.

    Only when ``DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR`` is explicitly truthy AND
    the process is not pointed at mainnet. On ``finney`` the flag is refused
    outright (logged at ERROR) so a stray dev env var can never open the
    validator surface on the production chain, defence-in-depth beyond keeping it
    unset in prod."""
    if os.environ.get("DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return False
    net = network.lower()
    if net.startswith("finney") or net == "mainnet":
        logger.error(
            "refusing DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR on production network=%s;"
            " enforcing the validator permit check",
            network,
        )
        return False
    return True


async def _assert_validator_permitted(
    chain: ChainClient, netuid: int, hotkey: str, *, network: str
) -> None:
    """Raise unless ``hotkey`` is a permitted validator on ``netuid``.

    A chain outage surfaces as 503 (matching the upload endpoints) rather
    than a silent allow/deny; a registered-but-unpermitted or unregistered
    hotkey is a :class:`ValidatorAuthError`. ``network`` is the resolved
    subtensor network, so the dev bypass can be refused on mainnet.
    """
    if _dev_bypass_permit(network):
        logger.warning(
            "DEV: allowing validator request without permit hotkey=%s netuid=%d",
            hotkey,
            netuid,
        )
        return
    try:
        neurons = await chain.get_recent_neurons(netuid)
    except ChainError as e:
        logger.warning(f"chain unreachable during validator authz: {e}")
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from e
    for neuron in neurons:
        if neuron.hotkey == hotkey:
            if neuron.validator_permit:
                return
            raise ValidatorAuthError(
                f"hotkey {hotkey} is registered but lacks a validator permit"
            )
    raise ValidatorAuthError(f"hotkey {hotkey} is not registered on netuid {netuid}")


async def require_validator(
    request: Request,
    chain: ChainDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate a validator GET via the ``X-Validator-Hotkey`` header.

    Verifies the header is a well-formed SS58 hotkey and that it is a
    permitted validator on the configured netuid. Returns the hotkey for
    logging/audit by the route.
    """
    if x_validator_hotkey is None or not re.fullmatch(
        _SS58_PATTERN, x_validator_hotkey
    ):
        raise ValidatorAuthError("missing or malformed X-Validator-Hotkey header")
    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, x_validator_hotkey, network=network
    )
    return x_validator_hotkey


ValidatorDep = Annotated[str, Depends(require_validator)]


def _lease_token(deadline: datetime) -> str:
    """Canonical UTC token that binds a score to one ticket lease."""
    return _aware_utc(deadline).isoformat(timespec="microseconds")


def _aware_utc(value: datetime) -> datetime:
    """Normalize database-naive UTC values for exact retry comparison."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _reported_transcript_sha256(report: ScoreReport) -> str | None:
    """The transcript digest a report declares, or ``None``.

    The scoring engine content-addresses the run's transcript artifact (the
    graded per-case inputs) and the validator forwards the digest under
    ``details["transcript_sha256"]``. ``details`` is otherwise opaque; this is
    the one key the platform reads back out of it at ingest, because the digest
    is bound into the signed payload (offline reproducibility, v3 review
    finding 3).
    """
    details = report.details if isinstance(report.details, dict) else {}
    value = details.get("transcript_sha256")
    if isinstance(value, str) and value:
        return value
    return None


def _reported_dataset_sha256(report: ScoreReport) -> str | None:
    """Return the canonical dataset digest declared by the scorer, if any."""
    details = report.details if isinstance(report.details, dict) else {}
    value = details.get("dataset_sha256")
    if isinstance(value, str) and value:
        return value
    return None


def _reported_v9_base_evidence(report: ScoreReport) -> V9BaseEvidence | None:
    """Return the already model-validated base root, when the report has one.

    Every confirmation-capable benchmark carries this root, so the cross-checks
    downstream must not stop applying the moment the network advances an epoch.
    It stays optional above the first such version: a scorer predating the
    carried-forward evidence contract is admissible without one (see
    ``ScoreReport._validate_v9_base_evidence``), which is why this reads the key
    defensively instead of indexing it.
    """
    if not supports_confirmation(report.bench_version):
        return None
    details = report.details if isinstance(report.details, dict) else {}
    raw = details.get("v9_base")
    if not isinstance(raw, dict):
        return None
    # ScoreReport's model validator has already checked this exact object and
    # its digest. Re-parse into the typed model so later identity comparisons do
    # not read authoritative fields from an opaque dictionary.
    return V9BaseEvidence.model_validate(raw)


def _score_details(
    report: ScoreReport, *, ticket_deadline: datetime, bench_version: int
) -> dict[str, Any]:
    """Build the persisted, retry-comparable telemetry for one score report."""
    details: dict[str, Any] = dict(report.details or {})
    details["ticket_deadline"] = _lease_token(ticket_deadline)
    details["bench_version"] = bench_version
    if report.composite_stderr is not None:
        details["composite_stderr"] = report.composite_stderr
    if report.raw_composite is not None:
        details["raw_composite"] = report.raw_composite
    if report.confirmation_composites is not None:
        details["confirmation_composites"] = report.confirmation_composites
    if report.confirmation_seeds is not None:
        details["confirmation_seeds"] = report.confirmation_seeds
    if report.base_evidence_sha256 is not None:
        details["base_evidence_sha256"] = report.base_evidence_sha256
    if report.per_case:
        details["per_case"] = [item.model_dump(mode="json") for item in report.per_case]
    return details


def _retry_details_match(
    stored: dict[str, Any] | None, reported: dict[str, Any]
) -> bool:
    """Compare signed score details while excluding platform-owned annotations.

    ``model_use`` is derived from the platform's inference ledger only after a
    score is accepted; it is neither supplied nor signed by the validator. An
    exact transport retry therefore cannot reproduce it. Every scorer-owned
    field remains part of the exact comparison, so this does not weaken the
    one-ticket/one-result guard.
    """
    if stored is None:
        return not reported
    comparable = dict(stored)
    comparable.pop("model_use", None)
    comparable.pop("platform_model_use_reconciliation", None)
    return comparable == reported


def _score_signing_message(
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime | None,
    report: ScoreReport,
) -> bytes:
    """Canonical bytes a score signature is verified against.

    Must match the validator's ``sign_score`` byte-for-byte:
    ``{validator_hotkey}:{agent_id}:{ticket_deadline}:{run_id}:``
    ``{composite!r}:{seed}`` — and, when the report declares a transcript
    digest, ``:{transcript_sha256}`` is appended (both sides derive presence
    from the same report field, so old validators that publish no transcript
    keep the previous format). Binding the exact lease means a response from an
    expired attempt cannot be replayed after the ticket is reissued; binding
    the transcript digest means the published artifact cannot be swapped after
    the fact without breaking the signature.
    """
    lease = _lease_token(ticket_deadline) if ticket_deadline is not None else ""
    # CANONICAL FIELD ORDER, mirrored byte-for-byte by ditto-subnet
    # ditto/validator/signing.py. Two independent changes each append a
    # conditional suffix here, so the order is fixed deliberately rather than
    # left to whichever merged first:
    #
    #   base : bench_version? : transcript_sha256? : base_evidence_sha256(v9)?
    #
    # bench_version sits next to seed because it QUALIFIES the seed -- the same
    # seed is a different dataset under a different contract -- so the "what was
    # scored" tuple stays contiguous. transcript_sha256 binds the artifact the
    # run PRODUCED, so it is outermost. A validator that sends neither produces
    # the pre-existing bytes, which is what keeps old validators verifiable.
    msg = (
        f"{validator_hotkey}:{agent_id}:{lease}:{report.run_id}:"
        f"{report.composite!r}:{report.seed}"
    )
    if report.bench_version is not None:
        msg += f":{report.bench_version}"
    transcript = _reported_transcript_sha256(report)
    if transcript:
        msg += f":{transcript}"
    if report.base_evidence_sha256:
        msg += f":{report.base_evidence_sha256}"
    return msg.encode()


def _job_signing_message(
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    slot_id: str | None = None,
) -> bytes:
    """Canonical bytes proving possession of a hotkey for one job claim."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    if slot_id is None:
        return f"validator-job:{validator_hotkey}:{nonce}:{requested}".encode()
    return (
        f"validator-job:v2:{validator_hotkey}:{slot_id}:{nonce}:{requested}"
    ).encode()


def _top5_confirmation_job_signing_message(
    validator_hotkey: str,
    nonce: UUID,
    requested_at: datetime,
    *,
    slot_id: str | None = None,
    champion_agent_id: UUID | None = None,
    member_agent_id: UUID | None = None,
) -> bytes:
    """Canonical proof-of-possession bytes for one top-five job claim."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    if slot_id is not None:
        return (
            "validator-top5-confirmation-job:v2:"
            f"{validator_hotkey}:{slot_id}:{nonce}:{requested}"
        ).encode()
    assert champion_agent_id is not None and member_agent_id is not None
    return (
        "validator-top5-confirmation-job:v1:"
        f"{validator_hotkey}:{champion_agent_id}:{member_agent_id}:"
        f"{nonce}:{requested}"
    ).encode()


def _top5_confirmation_score_signing_message(
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime,
    report: ScoreReport,
    *,
    bind_base_evidence: bool = True,
) -> bytes:
    """Bind every append-only seed/composite pair into a confirmation receipt.

    Protocol 19 uses v2 and binds the v9 base-evidence digest that authorizes
    continual token cost. ``bind_base_evidence=False`` reproduces v1 exactly so
    in-flight protocol-18 leases remain acceptable during rollout.
    """
    lease = _lease_token(ticket_deadline)
    pairs = list(
        zip(
            report.confirmation_seeds or [],
            report.confirmation_composites or [],
            strict=False,
        )
    )
    encoded_pairs = json.dumps(pairs, separators=(",", ":"))
    digest = report.base_evidence_sha256 if bind_base_evidence else None
    version = "v2" if digest else "v1"
    message = (
        f"validator-top5-confirmation-score:{version}:"
        f"{validator_hotkey}:{agent_id}:{lease}:{report.run_id}:"
        f"{report.bench_version}:{encoded_pairs}"
    )
    if digest:
        message += f":{digest}"
    return message.encode()


def _artifact_signing_message(
    validator_hotkey: str,
    agent_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Canonical proof-of-possession bytes for one artifact URL request."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-artifact:v1:{validator_hotkey}:{agent_id}:{nonce}:{requested}"
    ).encode()


def _job_fail_signing_message(
    validator_hotkey: str,
    agent_id: UUID,
    ticket_deadline: datetime,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Canonical proof-of-possession bytes for one ticket-fail request.

    Mirrored byte-for-byte by ditto-subnet ``ditto/validator/signing.py``. The
    lease ``ticket_deadline`` is bound so a captured fail request cannot close a
    later reissued ticket, and both timestamps use the same canonical UTC
    microsecond form as every other validator write.
    """
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"validator-job-fail:v1:{validator_hotkey}:{agent_id}:{deadline}:"
        f"{nonce}:{requested}"
    ).encode()


def _heartbeat_signing_message(
    *,
    validator_hotkey: str,
    software_version: str,
    protocol_version: int,
    code_digest: str,
    state: str,
    timestamp: int,
    active_agent_id: UUID | None = None,
    system_metrics: SystemMetrics | None = None,
    benchmark_progress: BenchmarkProgress | None = None,
    capabilities: ValidatorCapabilities | None = None,
    stack: ValidatorStackIdentity | None = None,
    stack_health: ValidatorStackHealth | None = None,
    benchmark_capacity: BenchmarkCapacity | None = None,
    confirmation_progress: list[ConfirmationProgress] | None = None,
    updater_status: ValidatorUpdaterStatus | None = None,
) -> bytes:
    """Canonical heartbeat payload, mirrored by ``ditto-subnet``."""
    if stack_health is not None and protocol_version < 9:
        raise ValueError("per-component stack health requires heartbeat protocol v9")
    if benchmark_capacity is not None and protocol_version < 10:
        raise ValueError("benchmark capacity requires heartbeat protocol v10")
    if confirmation_progress is not None and protocol_version < 22:
        raise ValueError("confirmation progress requires heartbeat protocol v22")
    if updater_status is not None and protocol_version < 23:
        raise ValueError("updater status requires heartbeat protocol v23")
    if protocol_version >= 10:
        if capabilities is None or stack is None or stack_health is None:
            raise ValueError(
                "heartbeat protocol v10 requires identity and stack health"
            )
        if benchmark_capacity is None:
            raise ValueError("heartbeat protocol v10 requires benchmark capacity")
        if protocol_version >= 22 and confirmation_progress is None:
            raise ValueError("heartbeat protocol v22 requires confirmation progress")
        if protocol_version >= 23:
            if updater_status is None:
                raise ValueError("heartbeat protocol v23 requires updater status")
            active = str(active_agent_id) if active_agent_id is not None else ""
            return (
                "ditto-validator-heartbeat:v23:"
                f"{validator_hotkey}:{software_version}:{protocol_version}:"
                f"{code_digest}:{state}:{active}:"
                f"{system_metrics_signing_token(system_metrics)}:"
                f"{benchmark_progress_signing_token(benchmark_progress)}:"
                f"{validator_identity_signing_token(capabilities, stack)}:"
                f"{validator_stack_health_signing_token(stack_health)}:"
                f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
                f"{confirmation_progress_signing_token(confirmation_progress)}:"
                f"{validator_updater_status_signing_token(updater_status)}:"
                f"{timestamp}"
            ).encode()
        if protocol_version >= 22:
            active = str(active_agent_id) if active_agent_id is not None else ""
            return (
                "ditto-validator-heartbeat:v22:"
                f"{validator_hotkey}:{software_version}:{protocol_version}:"
                f"{code_digest}:{state}:{active}:"
                f"{system_metrics_signing_token(system_metrics)}:"
                f"{benchmark_progress_signing_token(benchmark_progress)}:"
                f"{validator_identity_signing_token(capabilities, stack)}:"
                f"{validator_stack_health_signing_token(stack_health)}:"
                f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
                f"{confirmation_progress_signing_token(confirmation_progress)}:"
                f"{timestamp}"
            ).encode()
        active = str(active_agent_id) if active_agent_id is not None else ""
        signing_revision = "v11" if protocol_version >= 11 else "v10"
        return (
            f"ditto-validator-heartbeat:{signing_revision}:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:"
            f"{validator_stack_health_signing_token(stack_health)}:"
            f"{benchmark_capacity_signing_token(benchmark_capacity)}:"
            f"{timestamp}"
        ).encode()
    if protocol_version >= 9:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v9 requires capabilities and stack")
        if stack_health is None:
            raise ValueError("heartbeat protocol v9 requires stack health")
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v9:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:"
            f"{validator_stack_health_signing_token(stack_health)}:{timestamp}"
        ).encode()
    if protocol_version >= 8:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v8 requires capabilities and stack")
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v8:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:{timestamp}"
        ).encode()
    if protocol_version >= 7:
        if capabilities is None or stack is None:
            raise ValueError("heartbeat protocol v7 requires capabilities and stack")
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v7:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:"
            f"{validator_identity_signing_token(capabilities, stack)}:{timestamp}"
        ).encode()
    if protocol_version >= 4:
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v4:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:"
            f"{benchmark_progress_signing_token(benchmark_progress)}:{timestamp}"
        ).encode()
    if protocol_version >= 3:
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v3:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:"
            f"{system_metrics_signing_token(system_metrics)}:{timestamp}"
        ).encode()
    if protocol_version >= 2:
        active = str(active_agent_id) if active_agent_id is not None else ""
        return (
            "ditto-validator-heartbeat:v2:"
            f"{validator_hotkey}:{software_version}:{protocol_version}:"
            f"{code_digest}:{state}:{active}:{timestamp}"
        ).encode()
    return (
        "ditto-validator-heartbeat:v1:"
        f"{validator_hotkey}:{software_version}:{protocol_version}:"
        f"{code_digest}:{state}:{timestamp}"
    ).encode()


def _verify_signature(hotkey: str, payload: bytes, signature_hex: str) -> bool:
    """Return True iff ``signature_hex`` is a valid sr25519 sig over ``payload``.

    Mirrors the upload endpoint's verification: a narrow ``(ValueError,
    TypeError)`` catch covers malformed hex / SS58 / wrong-shape inputs;
    anything else is a programming bug that should surface as a 500.
    """
    try:
        keypair = bittensor.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(payload, bytes.fromhex(signature_hex)))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class _HeartbeatWork:
    """The ticket-validated work a heartbeat claims, ready to persist.

    Separate from the *liveness* half of a heartbeat (``seen_at`` /
    ``reported_at`` / identity / health), which is proven by the signature alone
    and must be storable even when nothing here can be derived.
    """

    active_agent_id: UUID | None
    benchmark_progress: dict | None
    benchmark_capacity: BenchmarkCapacity | None
    confirmation_progress: list[dict] | None
    # The signed occupancy claim, kept whole even when confirmation narrows
    # ``benchmark_capacity``. Never used to grant work or accept a score --
    # only to refuse a revocation. See ``ValidatorHeartbeat.claimed_slots``.
    claimed_slots: list[dict] | None = None


# What a heartbeat stores when its work payload cannot be validated: alive, but
# making no claim about what it is running.
_LIVENESS_ONLY_WORK = _HeartbeatWork(
    active_agent_id=None,
    benchmark_progress=None,
    benchmark_capacity=None,
    confirmation_progress=None,
    claimed_slots=None,
)


def _claimed_slots(capacity: BenchmarkCapacity | None) -> list[dict] | None:
    """Project the signed capacity onto its bare (slot, agent) occupancy claim."""
    if capacity is None:
        return None
    return [
        {"slot_id": slot.slot_id, "agent_id": str(slot.agent_id)}
        for slot in capacity.active
    ]


# Heartbeat protocol 17 adds the authoritative lease roster to the *response*.
# The platform owns lease assignment, so it is the only party that can tell a
# reporter its lease is gone; before v17 a revoked lease was discoverable only
# by a 409 at score time, i.e. after a full benchmark had already been burned.
_LEASE_ROSTER_PROTOCOL = 17


async def _lease_roster(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    protocol_version: int,
    now: datetime,
) -> list[HeldLease] | None:
    """Enumerate the leases the ledger holds for this validator, or ``None``.

    ``None`` is "the platform is not answering" and is the only safe default.
    It is returned when the reporter declared a protocol below v17 — an older
    validator must behave exactly as it does today — and whenever the read
    itself failed. It is emphatically *not* the same value as ``[]``, which is
    the authoritative "you hold no lease" a reporter may act on. A reporter that
    treated a failed read as an empty roster would kill every run on the fleet
    the first time this query threw, which is the failure mode #437/#443/#496
    were each written to prevent.

    Runs in its own savepoint so a failure here rolls back only itself: the
    liveness half of a heartbeat must survive anything that goes wrong while
    describing work, exactly as it does for the work-validation path above.
    """
    if protocol_version < _LEASE_ROSTER_PROTOCOL:
        return None
    try:
        async with session.begin_nested():
            tickets = await list_validator_live_leases(
                session, validator_hotkey=validator_hotkey, now=now
            )
    except Exception as error:  # noqa: BLE001 - a failed read must not stop work
        VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED.labels(
            stage="lease_roster", reason=type(error).__name__
        ).inc()
        logger.exception(
            "validator heartbeat answered without a lease roster after the read "
            "failed validator=%s protocol=%s",
            validator_hotkey,
            protocol_version,
        )
        return None
    return [
        HeldLease(
            slot_id=ticket.slot_id,
            agent_id=ticket.agent_id,
            bench_version=ticket.bench_version,
            deadline=_as_utc_deadline(ticket.deadline),
        )
        for ticket in tickets
    ]


def _as_utc_deadline(value: datetime) -> datetime:
    """Stamp a naive stored deadline as UTC so the wire is unambiguous."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _validated_heartbeat_work(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    request_body: ValidatorHeartbeatRequest,
    now: datetime,
) -> _HeartbeatWork:
    """Project the signed work claim onto what the ledger actually agrees with.

    Every slot is confirmed against a live ticket and a scoreable agent using
    ordinary MVCC reads.  Heartbeats are observational: briefly retaining work
    whose lease is consumed concurrently is conservative (it cannot issue or
    score a ticket), and the next heartbeat removes it.  Taking the same ticket
    row locks as inference accounting made one chat-heavy agent delay the whole
    validator heartbeat and hide progress for all of its slots.

    The optional first-report stamp is the sole write here.  It reacquires the
    ticket with ``SKIP LOCKED`` so accounting or scoring always wins; a skipped
    stamp is retried by the next heartbeat and leaves the lease on the safer,
    not-yet-revocable side of the liveness gate.
    """
    stored_active_agent_id = request_body.active_agent_id
    stored_benchmark_progress = (
        request_body.benchmark_progress.model_dump(mode="json")
        if request_body.benchmark_progress is not None
        else None
    )
    stored_benchmark_capacity = request_body.benchmark_capacity
    stored_confirmation_progress: list[dict] | None = None
    if request_body.confirmation_progress is not None:
        stored_confirmation_progress = []
        ticket_ids = {
            progress.ticket_id for progress in request_body.confirmation_progress
        }
        confirmation_rows = (
            await session.execute(
                select(ConfirmationBundleTicket, ConfirmationBundleSubject.agent_id)
                .join(
                    ConfirmationBundleSubject,
                    ConfirmationBundleSubject.bundle_id
                    == ConfirmationBundleTicket.bundle_id,
                )
                .where(
                    ConfirmationBundleTicket.ticket_id.in_(ticket_ids),
                    ConfirmationBundleTicket.validator_hotkey == validator_hotkey,
                    ConfirmationBundleTicket.status == "issued",
                    ConfirmationBundleTicket.deadline > now,
                )
            )
        ).all()
        valid_confirmation_identities = {
            (
                ticket.ticket_id,
                ticket.bundle_id,
                ticket.slot_id,
                agent_id,
                _as_utc_deadline(ticket.deadline),
            )
            for ticket, agent_id in confirmation_rows
        }
        for progress in request_body.confirmation_progress:
            identity = (
                progress.ticket_id,
                progress.bundle_id,
                progress.slot_id,
                progress.agent_id,
                progress.ticket_deadline,
            )
            if identity in valid_confirmation_identities:
                stored_confirmation_progress.append(progress.model_dump(mode="json"))
            else:
                logger.info(
                    "validator heartbeat dropped stale confirmation progress "
                    "validator=%s slot=%s ticket=%s",
                    validator_hotkey,
                    progress.slot_id,
                    progress.ticket_id,
                )
    # Captured BEFORE the confirmation filter below. A slot that fails to confirm
    # is dropped from the stored capacity, and the lease liveness gate reads that
    # absence as positive evidence the slot is idle -- so without this the filter
    # can hand the revoker a false idle verdict for a run that is very much alive.
    claimed = _claimed_slots(stored_benchmark_capacity)
    if stored_benchmark_capacity is not None:
        previous_heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
        previous_slots = {}
        if previous_heartbeat is not None and isinstance(
            previous_heartbeat.benchmark_capacity, dict
        ):
            with contextlib.suppress(ValidationError):
                previous_capacity = BenchmarkCapacity.model_validate(
                    previous_heartbeat.benchmark_capacity
                )
                previous_slots = {
                    slot.slot_id: slot for slot in previous_capacity.active
                }
        valid_active = []
        slot_identities = [
            (slot.slot_id, slot.agent_id) for slot in stored_benchmark_capacity.active
        ]
        agents_by_id = (
            {
                agent.agent_id: agent
                for agent in await session.scalars(
                    select(Agent).where(
                        Agent.agent_id.in_(
                            {agent_id for _slot_id, agent_id in slot_identities}
                        )
                    )
                )
            }
            if slot_identities
            else {}
        )
        tickets_by_slot = await list_live_slot_tickets(
            session,
            validator_hotkey=validator_hotkey,
            slots=slot_identities,
            now=now,
        )
        valid_identities = {
            identity
            for identity, ticket in tickets_by_slot.items()
            if ticket is not None
            and (agent := agents_by_id.get(identity[1])) is not None
            and agent.status in _SCOREABLE_STATUSES
        }
        stampable = await list_live_slot_tickets(
            session,
            validator_hotkey=validator_hotkey,
            slots=valid_identities,
            now=now,
            first_report_unstamped_only=True,
            for_update_skip_locked=True,
        )
        for stamp_ticket in stampable.values():
            stamp_ticket.first_reported_at = now
        for slot in stored_benchmark_capacity.active:
            agent = agents_by_id.get(slot.agent_id)
            # Identity, not deadline stamp. A re-issued lease moves the deadline
            # the validator cached, and matching on it evicted a live slot from
            # the stored capacity: its progress vanished from the fleet view and
            # the revoker then read the absence as proof the slot was idle.
            slot_ticket = tickets_by_slot.get((slot.slot_id, slot.agent_id))
            if (
                slot_ticket is not None
                and agent is not None
                and agent.status in _SCOREABLE_STATUSES
            ):
                previous_slot = previous_slots.get(slot.slot_id)
                if previous_slot is not None:
                    try:
                        _validate_same_lease_progress(
                            previous_slot.progress, slot.progress
                        )
                    except HeartbeatProgressRegressionError:
                        slot = previous_slot
                valid_active.append(slot)
            else:
                logger.info(
                    "validator heartbeat dropped stale slot progress "
                    "validator=%s slot=%s",
                    validator_hotkey,
                    slot.slot_id,
                )
        valid_active.sort(key=lambda slot: slot.slot_id)
        # Every slot the validator claimed was dropped, yet it says it is running
        # a benchmark. The per-slot lines above are INFO and one-per-slot, so this
        # pattern -- a healthy validator whose work is entirely unmatchable --
        # produced no aggregate signal at all, and the fleet view then renders it
        # identically to a genuinely idle validator. That is what made a retest
        # reporting against the wrong slot indistinguishable from a wedged one.
        # Warn, never reject: a 422 here would skip the handler entirely and
        # freeze ``seen_at``, which is the input to lease force-expiry.
        if claimed and not valid_active and request_body.state == "running_benchmark":
            logger.warning(
                "validator heartbeat claims running_benchmark but no claimed "
                "slot matches a live lease; the fleet view will read this "
                "validator as idle validator=%s claimed=%s",
                validator_hotkey,
                claimed,
            )
        stored_benchmark_capacity = stored_benchmark_capacity.model_copy(
            update={"active": valid_active}
        )
        primary = (
            sorted(valid_active, key=lambda slot: slot.slot_id)[0]
            if valid_active
            else None
        )
        stored_active_agent_id = primary.agent_id if primary is not None else None
        stored_benchmark_progress = (
            # None when a v16 reporter has claimed the slot but has nothing to
            # report yet. The legacy scalar mirrors the primary slot, so it is
            # absent for the same reason the slot's own progress is.
            primary.progress.model_dump(mode="json")
            if primary is not None and primary.progress is not None
            else None
        )
    if (
        stored_benchmark_capacity is None
        and request_body.benchmark_progress is not None
    ):
        assert request_body.active_agent_id is not None
        agent = await get_agent_by_id(session, agent_id=request_body.active_agent_id)
        ticket = await get_open_ticket(
            session,
            agent_id=request_body.active_agent_id,
            validator_hotkey=validator_hotkey,
            now=now,
            deadline=request_body.benchmark_progress.ticket_deadline,
            bench_version=None,
        )
        if ticket is None or agent is None or agent.status not in _SCOREABLE_STATUSES:
            # Ticket-bound progress is optional decoration. A benchmark can
            # outlive or lose its lease, but that must not discard an
            # otherwise valid signed liveness/health report. Persist the
            # authenticated fail-open projection without stale work context;
            # tickets, submissions, benchmarks, and scores are untouched.
            stored_active_agent_id = None
            stored_benchmark_progress = None
            logger.info(
                "validator heartbeat dropped stale ticket-bound progress validator=%s",
                validator_hotkey,
            )
    return _HeartbeatWork(
        active_agent_id=stored_active_agent_id,
        benchmark_progress=stored_benchmark_progress,
        benchmark_capacity=stored_benchmark_capacity,
        confirmation_progress=stored_confirmation_progress,
        claimed_slots=claimed,
    )


@router.post(
    "/heartbeat",
    response_model=ValidatorHeartbeatResponse,
    responses={
        401: {"description": "Invalid permit, identity, signature, or timestamp."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def heartbeat(
    request: Request,
    request_body: ValidatorHeartbeatRequest,
    validator_hotkey: ValidatorDep,
    session: SessionDep,
) -> ValidatorHeartbeatResponse:
    """Record a fresh, signed proof of the worker bytes serving this hotkey."""
    content_length = request.headers.get("content-length")
    try:
        claimed_bytes = int(content_length) if content_length is not None else 0
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from error
    if (
        claimed_bytes > _HEARTBEAT_MAX_BYTES
        or len(await request.body()) > _HEARTBEAT_MAX_BYTES
    ):
        raise HTTPException(status_code=413, detail="heartbeat payload too large")
    if request_body.validator_hotkey != validator_hotkey:
        raise ValidatorAuthError("heartbeat body hotkey does not match header")

    now = datetime.now(UTC)
    if abs(int(now.timestamp()) - request_body.timestamp) > _HEARTBEAT_MAX_SKEW_SECONDS:
        raise ValidatorAuthError(
            "heartbeat timestamp is stale or too far in the future"
        )
    if request_body.protocol_version < 2 and request_body.active_agent_id is not None:
        raise ValidatorAuthError("heartbeat protocol v1 cannot report active work")
    if request_body.protocol_version < 3 and request_body.system_metrics is not None:
        raise ValidatorAuthError("system metrics require heartbeat protocol v3")
    if (
        request_body.protocol_version < 4
        and request_body.benchmark_progress is not None
    ):
        raise ValidatorAuthError("benchmark progress requires heartbeat protocol v4")
    if request_body.benchmark_progress is not None and (
        request_body.active_agent_id is None
        or request_body.state != "running_benchmark"
    ):
        raise ValidatorAuthError(
            "benchmark progress requires active running_benchmark work"
        )
    if (
        request_body.system_metrics is not None
        and abs(request_body.timestamp - request_body.system_metrics.collected_at)
        > _HEARTBEAT_MAX_SKEW_SECONDS
    ):
        raise ValidatorAuthError(
            "system metrics timestamp is outside the heartbeat window"
        )
    if (
        request_body.updater_status is not None
        and abs(request_body.timestamp - request_body.updater_status.observed_at)
        > _HEARTBEAT_MAX_SKEW_SECONDS
    ):
        raise ValidatorAuthError(
            "updater status timestamp is outside the heartbeat window"
        )
    if (
        request_body.active_agent_id is not None
        and request_body.state != "running_benchmark"
    ):
        raise ValidatorAuthError("active agent requires running_benchmark state")
    # Self-contradictory but deliberately ACCEPTED: under v10+ the wire model
    # constrains ``state`` only when ``active`` is non-empty, so a validator can
    # sign "running_benchmark" while declaring no occupied slot. The lease gate
    # ignores ``state`` under v10+ and reads the empty capacity as positive
    # evidence of idleness, so this payload can get a live run's slot revoked.
    #
    # This is intentionally a warning and not a ``ValidatorAuthError``. Rejecting
    # it -- here or in the wire model -- would stop refreshing ``seen_at`` for
    # every validator still emitting the shape, and a frozen ``seen_at`` is the
    # input to lease force-expiry. Tighten only once fleet logs show this line
    # has gone quiet; see the PR body for the ordering.
    if (
        request_body.protocol_version >= 10
        and request_body.state == "running_benchmark"
        and request_body.benchmark_capacity is not None
        and not request_body.benchmark_capacity.active
    ):
        logger.warning(
            "validator heartbeat is self-contradictory: state=running_benchmark "
            "with an empty v%d capacity; the lease gate will read this as idle "
            "validator=%s",
            request_body.protocol_version,
            validator_hotkey,
        )
    payload = _heartbeat_signing_message(
        validator_hotkey=validator_hotkey,
        software_version=request_body.software_version,
        protocol_version=request_body.protocol_version,
        code_digest=request_body.code_digest,
        state=request_body.state,
        timestamp=request_body.timestamp,
        active_agent_id=request_body.active_agent_id,
        system_metrics=request_body.system_metrics,
        benchmark_progress=request_body.benchmark_progress,
        capabilities=request_body.capabilities,
        stack=request_body.stack,
        stack_health=request_body.stack_health,
        benchmark_capacity=request_body.benchmark_capacity,
        confirmation_progress=request_body.confirmation_progress,
        updater_status=request_body.updater_status,
    )
    if not _verify_signature(validator_hotkey, payload, request_body.signature):
        raise ValidatorAuthError("heartbeat signature verification failed")

    reported_at = datetime.fromtimestamp(request_body.timestamp, tz=UTC)
    async with session.begin():
        try:
            # SAVEPOINT, not a bare try: a database-level failure while deriving
            # the optional work payload would otherwise poison the surrounding
            # transaction and take the liveness write down with it anyway.
            # The only lock this path now attempts is a best-effort SKIP LOCKED
            # first-report stamp; ticket accounting and scoring never wait on a
            # heartbeat.
            async with session.begin_nested():
                work = await _validated_heartbeat_work(
                    session,
                    validator_hotkey=validator_hotkey,
                    request_body=request_body,
                    now=now,
                )
        except Exception as error:  # noqa: BLE001 - liveness must not depend on payload
            # Fail OPEN on liveness. The signature already proved this validator
            # is alive and on schedule; whatever went wrong concerns only what it
            # *claims to be doing*. Storing the liveness-only projection keeps
            # `seen_at` moving so the fleet does not read the validator as
            # heartbeat_stale, and drops the work payload rather than freezing
            # the previous one — a frozen capacity blob is what let a lease
            # revocation destroy healthy v7 runs (#437). With capacity NULL the
            # next `/job` claim fails closed with 428 (fresh valid benchmark
            # capacity is required) instead of revoking live work.
            # The occupancy claim comes straight off the verified signature and
            # needs no ledger read, so it survives a failed work validation. It
            # only ever refuses a revocation, so keeping it is the safe side.
            work = replace(
                _LIVENESS_ONLY_WORK,
                claimed_slots=_claimed_slots(request_body.benchmark_capacity),
            )
            VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED.labels(
                stage="work_validation", reason=type(error).__name__
            ).inc()
            logger.exception(
                "validator heartbeat stored liveness-only after work payload "
                "validation failed validator=%s protocol=%s state=%s",
                validator_hotkey,
                request_body.protocol_version,
                request_body.state,
            )
        # Progress monotonicity is enforced fail-open inside the query: a
        # genuine same-run regression keeps the previously stored progress
        # (never moving the public display backward) but never rejects an
        # authenticated liveness report, and a fresh run_token rebaselines.
        row, accepted = await upsert_validator_heartbeat(
            session,
            validator_hotkey=validator_hotkey,
            software_version=request_body.software_version,
            protocol_version=request_body.protocol_version,
            code_digest=request_body.code_digest,
            state=request_body.state,
            active_agent_id=work.active_agent_id,
            system_metrics=(
                request_body.system_metrics.model_dump(mode="json")
                if request_body.system_metrics is not None
                else None
            ),
            benchmark_progress=work.benchmark_progress,
            capabilities=(
                request_body.capabilities.model_dump(mode="json", exclude_none=True)
                if request_body.capabilities is not None
                else None
            ),
            stack=(
                request_body.stack.model_dump(mode="json")
                if request_body.stack is not None
                else None
            ),
            stack_health=(
                request_body.stack_health.model_dump(mode="json", exclude_none=True)
                if request_body.stack_health is not None
                else None
            ),
            updater_status=(
                request_body.updater_status.model_dump(mode="json", exclude_none=True)
                if request_body.updater_status is not None
                else None
            ),
            benchmark_capacity=(
                work.benchmark_capacity.model_dump(mode="json")
                if work.benchmark_capacity is not None
                else None
            ),
            confirmation_progress=work.confirmation_progress,
            claimed_slots=work.claimed_slots,
            reported_at=reported_at,
            seen_at=now,
            signature=request_body.signature,
        )
        # Read after the upsert and inside the same transaction, so the roster
        # the reporter acts on is consistent with the heartbeat just stored.
        leases = await _lease_roster(
            session,
            validator_hotkey=validator_hotkey,
            protocol_version=request_body.protocol_version,
            now=now,
        )
    seen_at = row.seen_at
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    return ValidatorHeartbeatResponse(accepted=accepted, seen_at=seen_at, leases=leases)


@router.post(
    "/job",
    response_model=JobResponse,
    responses={
        204: {"description": "No agent needs this validator right now."},
        401: {"description": "Missing/invalid validator auth."},
        426: {"description": "Validator software or protocol must be upgraded."},
        428: {"description": "A fresh signed validator heartbeat is required."},
        409: {"description": "Stale or replayed signed job claim."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def request_job(
    payload: JobRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    generator: GeneratorDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> JobResponse | Response:
    """Issue this validator a scoring ticket for the next eligible agent.

    The k=3 pull: at most :data:`SCORING_QUORUM` tickets per agent go to that
    many distinct validators, so most requests get **204 No Content** ("no job
    for you"). An issued ticket must be redeemed with a score before its
    deadline, or it lapses and the slot re-opens for another validator. The
    ticket write (and the overdue-ticket sweep it runs) commit together.
    """
    # Prove the caller owns the hotkey before it can reserve a scarce quorum
    # slot. The header remains for consistent routing/audit but must match the
    # signed body exactly.
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError("job claim header does not match signed hotkey")
    signed = _job_signing_message(
        payload.validator_hotkey,
        payload.nonce,
        payload.requested_at,
        payload.slot_id,
    )
    if not _verify_signature(payload.validator_hotkey, signed, payload.signature):
        raise ValidatorAuthError(
            f"job claim signature did not verify for hotkey {payload.validator_hotkey}"
        )
    now = datetime.now(UTC)
    requested_at = payload.requested_at.astimezone(UTC)
    if abs(now - requested_at) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(status_code=409, detail="job claim timestamp is stale")

    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, payload.validator_hotkey, network=network
    )

    # Resolved before the transaction opens: the resolver may open its own
    # session to refill a cold cache, and doing that inside this transaction
    # would nest sessions on the hot path.
    queue_policy = await _resolve_queue_policy(request)
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    # Queue fifth/tenth-place admission floors consume the current epoch's
    # efficiency-adjusted canonical order. Materialize that frozen epoch here,
    # before the job transaction and before any floor read, so a new UTC epoch
    # cannot issue work against a temporary raw-score order while waiting for a
    # leaderboard or scoring-ledger request to create its assignments.
    if efficiency_config.enabled:
        await ensure_current_efficiency_state(
            request.app.state, session, efficiency_config, now=now
        )
    # Resolve the operator slot cap on the resolver's own session, before the
    # request transaction opens: reading it on `session` here would autobegin and
    # break the `session.begin()` below.
    slot_settings = await _validator_slot_settings(request)
    # Same reason, same rule: the chat request budget stamped onto a new grant
    # comes from the operator board, and that resolver reads on its own session.
    inference_config = await resolved_proxy_config(
        request.app.state, request.app.state.config.inference_proxy
    )
    inference_settings = await request.app.state.inference_concurrency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )

    job: JobResponse | None = None
    async with session.begin():
        # The allocator has several independently-correct lanes whose row-lock
        # orders are not interchangeable (ordinary tickets, rollout members,
        # and queued score re-tests).  Fence the complete dispatch transaction
        # before the first one can lock a row.  Pollers are retry loops, so a
        # busy fence is a cheap 204 rather than another waiter extending the
        # control-plane saturation incident.
        if not await try_lock_rollout_dispatch(session):
            _record_dispatch_decline(
                "allocator_busy",
                validator_hotkey=payload.validator_hotkey,
                slot_id=payload.slot_id or "slot-0",
            )
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        await _assert_validator_compatible(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            config=request.app.state.config.validator_compatibility,
        )
        artifact_mode, validator_state = await _validator_artifact_routing(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            heartbeat_max_age_seconds=(
                request.app.state.config.validator_compatibility.heartbeat_max_age_seconds
            ),
        )
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _JOB_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as exc:
            raise HTTPException(
                status_code=409, detail="job claim nonce has already been used"
            ) from exc
        heartbeat = await session.get(ValidatorHeartbeat, payload.validator_hotkey)
        canonical_version = await active_bench_version(session)
        rollout = await open_rollout(session)
        source_backfill_rollout = rollout
        if source_backfill_rollout is None and canonical_version >= 7:
            source_backfill_rollout = await activated_rollout_for_version(
                session, bench_version=canonical_version
            )
        target_version = (
            rollout.desired_version if rollout is not None else canonical_version
        )
        inference_required = (
            request.app.state.config.inference_proxy.required or target_version >= 7
        )
        heartbeat_capabilities: ValidatorCapabilities | None = None
        v7_calibration = None
        target_inference_ready = True
        if inference_required:
            try:
                heartbeat_capabilities = ValidatorCapabilities.model_validate_json(
                    json.dumps(
                        heartbeat.capabilities if heartbeat is not None else None
                    )
                )
            except ValidationError:
                target_inference_ready = False
            if (
                heartbeat_capabilities is not None
                and heartbeat_capabilities.scorer_benchmarks is not None
            ):
                v7_calibration = heartbeat_capabilities.scorer_benchmarks.v7_calibration
            if heartbeat_capabilities is None or (
                heartbeat is None
                or heartbeat.protocol_version < (11 if target_version >= 7 else 10)
                or not heartbeat_capabilities.ticket_inference
                or (
                    target_version == 7
                    and (
                        heartbeat_capabilities.scorer_benchmarks is None
                        or heartbeat_capabilities.scorer_benchmarks.v7_calibration
                        is None
                    )
                )
            ):
                target_inference_ready = False
        # One authority for every lane that can issue desired-era work.
        #
        # The rollout helpers already validate the signed scorer capability,
        # but the fresh-submission fallback below calls ``issue_ticket``
        # directly.  Gating that fallback only on inference readiness let a
        # scorer rejected by the semantic release floor fall through the
        # cohort helper and immediately receive the same v9 work here.  Keep
        # inference and scorer eligibility coupled once, before any rollout
        # lane can reserve a ticket.
        target_benchmark_ready = (
            target_inference_ready
            and heartbeat is not None
            and heartbeat_supports_version(heartbeat, now=now, version=target_version)
        )
        slot_id = payload.slot_id or "slot-0"
        issuance_paused = validator_issuance_paused(
            slot_settings, validator_hotkey=payload.validator_hotkey
        )
        if issuance_paused and slot_id not in await _held_lease_slots(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
        ):
            # Pause is issuance-only. An existing ticket on this slot continues
            # through the ordinary resume path and may still submit its result;
            # an actually idle slot receives nothing from any lane below.
            _record_dispatch_decline(
                "validator_paused",
                validator_hotkey=payload.validator_hotkey,
                slot_id=slot_id,
            )
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        slot_running_benchmark = validator_state == "running_benchmark"
        if heartbeat is not None and heartbeat.protocol_version >= 10:
            if payload.slot_id is None:
                raise HTTPException(
                    status_code=409, detail="heartbeat v10 job claims require slot_id"
                )
            try:
                capacity = BenchmarkCapacity.model_validate(
                    heartbeat.benchmark_capacity
                )
            except ValidationError as error:
                raise HTTPException(
                    status_code=428,
                    detail="fresh valid benchmark capacity is required",
                ) from error
            slot_running_benchmark = any(
                slot.slot_id == slot_id for slot in capacity.active
            )
            if (
                capacity.admission != "accepting"
                or slot_id not in capacity.healthy_slots
            ):
                # Two distinct operator stories share one gate: the whole
                # validator has closed admission (a drain, an upgrade), or the
                # validator is up but is not offering *this* slot. Split them on
                # the way out so an idle fleet reads as one or the other.
                _record_dispatch_decline(
                    "not_accepting"
                    if capacity.admission != "accepting"
                    else "slot_not_healthy",
                    validator_hotkey=payload.validator_hotkey,
                    slot_id=slot_id,
                )
                return Response(status_code=204)
            # Operator slot cap. Validators advertise the capacity their host
            # can offer; how much of it the fleet actually uses is an operator
            # decision that must be changeable from backroom without a release.
            # The ceiling is on how many leases one validator holds at once, so
            # it is enforced by counting the live ones -- not by refusing every
            # slot ordinal at or above the cap, which silently under-fills any
            # validator whose healthy slots are sparse (see
            # ``_slot_cap_declines``).
            #
            # A slot already running a benchmark is exempt, and every path that
            # resumes a live lease is downstream of this gate: without the
            # exemption, lowering the cap would strand the in-flight leases it
            # no longer covers until they expired, burning a retry attempt
            # each. Lowering the cap must cost the fleet nothing but new work.
            # The exemption cannot be forged -- heartbeat ingest drops any
            # active slot with no matching open ticket -- so ``capacity.active``
            # only ever names slots the platform itself leased.
            resource_sample = _heartbeat_resource_sample(heartbeat)
            allowed_slots = allowed_slot_count(
                slot_settings,
                advertised_slots=capacity.configured_slots,
                sample=resource_sample,
            )
            if _slot_cap_declines(
                slot_id=slot_id,
                slot_running_benchmark=slot_running_benchmark,
                allowed_slots=allowed_slots,
                held_slots=await _held_lease_slots(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                ),
            ):
                # Same disk sample the decision used, so the label can never
                # describe a different heartbeat than the refusal did.
                _record_dispatch_decline(
                    _slot_cap_decline_reason(
                        slot_id=slot_id,
                        settings=slot_settings,
                        advertised_slots=capacity.configured_slots,
                        sample=resource_sample,
                    ),
                    validator_hotkey=payload.validator_hotkey,
                    slot_id=slot_id,
                )
                return Response(status_code=204)
            if target_version >= 7 and _inference_stage_cap_declines(
                slot_id=slot_id,
                slot_running_benchmark=slot_running_benchmark,
                allowed_slots=_inference_stage_slot_cap(inference_config),
                capacity=capacity,
            ):
                _record_dispatch_decline(
                    "inference_slot_cap",
                    validator_hotkey=payload.validator_hotkey,
                    slot_id=slot_id,
                )
                return Response(status_code=204)
        if rollout is not None:
            # A shadow/mismatched v9 score cannot satisfy rollout activation,
            # so its operator-authorized replacement is rollout work rather
            # than idle backfill. Dispatch only that typed basis here;
            # statistical outlier re-tests keep their lower-priority behavior
            # below, after ordinary work and only once no rollout is open.
            ticket = (
                await activate_next_score_retest(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    supports_version=lambda version: (
                        heartbeat is not None
                        and version == rollout.desired_version
                        and heartbeat_supports_version(
                            heartbeat, now=now, version=version
                        )
                    ),
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                    required_basis=V9_CONTRACT_RETEST_BASIS,
                    allow_parallel_ordinary=True,
                    allow_parallel_contract_retests=True,
                )
                if target_benchmark_ready
                else None
            )
            # The activation boundary waits for every frozen member, not just
            # the first-five authority subset.  Give that bounded cohort first
            # refusal on every capable slot while the rollout is open.  The
            # rollout allocator already returns ``None`` when this validator
            # has scored, holds, or exhausted every member it can advance, so
            # ordinary desired-era work still fills otherwise-idle capacity.
            if ticket is None and target_benchmark_ready:
                ticket = await issue_rollout_ticket(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    ttl=_TICKET_TTL,
                    artifact_mode=artifact_mode,
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                )
            fresh_lane_due = (
                ticket is None
                and target_benchmark_ready
                and await _fresh_submission_lane_due(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    bench_version=rollout.desired_version,
                    rollout_started_at=rollout.created_at,
                    now=now,
                    settings=queue_policy,
                )
            )
            relaxed_carryover_due = _prev_gen_carryover_precedes_desired_era(
                fresh_lane_due=fresh_lane_due,
                settings=queue_policy.prev_gen_carryover,
            )
            ticket = ticket or (
                await _issue_prev_gen_carryover_ticket(
                    session,
                    rollout=rollout,
                    heartbeat=heartbeat,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    settings=queue_policy.prev_gen_carryover,
                    target_inference_ready=target_benchmark_ready,
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                    efficiency_config=efficiency_config,
                    owner_concurrent_submission_limit=(
                        queue_policy.owner_concurrent_submission_limit
                    ),
                    similarity_policy=policy_from_settings(
                        queue_policy.similarity_budget
                    ),
                    similarity_concurrent_submission_limit=(
                        queue_policy.similarity_budget.concurrent_submission_limit
                    ),
                )
                if relaxed_carryover_due
                else None
            )
            if ticket is None:
                ticket = (
                    await issue_ticket(
                        session,
                        validator_hotkey=payload.validator_hotkey,
                        now=now,
                        ttl=_TICKET_TTL,
                        bench_version=rollout.desired_version,
                        artifact_mode="screened_only",
                        validator_running_benchmark=slot_running_benchmark,
                        submitted_at_or_after=rollout.created_at,
                        fifo_start_at=rollout.created_at,
                        completion_first=True,
                        slot_id=slot_id,
                        efficiency_config=efficiency_config,
                        owner_concurrent_submission_limit=(
                            queue_policy.owner_concurrent_submission_limit
                        ),
                        similarity_policy=policy_from_settings(
                            queue_policy.similarity_budget
                        ),
                        similarity_concurrent_submission_limit=(
                            queue_policy.similarity_budget.concurrent_submission_limit
                        ),
                    )
                    if fresh_lane_due
                    else None
                )
            if ticket is None and not fresh_lane_due and target_benchmark_ready:
                ticket = await issue_ticket(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    ttl=_TICKET_TTL,
                    bench_version=rollout.desired_version,
                    artifact_mode="screened_only",
                    validator_running_benchmark=slot_running_benchmark,
                    submitted_at_or_after=rollout.created_at,
                    fifo_start_at=rollout.created_at,
                    completion_first=True,
                    slot_id=slot_id,
                    efficiency_config=efficiency_config,
                    owner_concurrent_submission_limit=(
                        queue_policy.owner_concurrent_submission_limit
                    ),
                    similarity_policy=policy_from_settings(
                        queue_policy.similarity_budget
                    ),
                    similarity_concurrent_submission_limit=(
                        queue_policy.similarity_budget.concurrent_submission_limit
                    ),
                )
            if ticket is None and not fresh_lane_due and not relaxed_carryover_due:
                ticket = await _issue_prev_gen_carryover_ticket(
                    session,
                    rollout=rollout,
                    heartbeat=heartbeat,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    settings=queue_policy.prev_gen_carryover,
                    target_inference_ready=target_benchmark_ready,
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                    efficiency_config=efficiency_config,
                    owner_concurrent_submission_limit=(
                        queue_policy.owner_concurrent_submission_limit
                    ),
                    similarity_policy=policy_from_settings(
                        queue_policy.similarity_budget
                    ),
                    similarity_concurrent_submission_limit=(
                        queue_policy.similarity_budget.concurrent_submission_limit
                    ),
                )
        else:
            ticket = None
        if ticket is None and rollout is None:
            # Resume only the active benchmark when there is no open rollout.
            # An open rollout is an exclusive desired-era transition: the v9
            # lanes above either issue v9 or intentionally return no work.
            # Resuming the still-active source era here bypassed every
            # carryover/drain control and let old v8 repairs consume slots while
            # v9 was collecting. Existing source tickets remain in the ledger
            # and age out naturally; they are never re-leased during rollout.
            live_ticket_statement = (
                select(ValidatorTicket)
                .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
                .where(
                    ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                    ValidatorTicket.slot_id == slot_id,
                    ValidatorTicket.bench_version == canonical_version,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
                    ValidatorTicket.purpose_revision > 0,
                    ValidatorTicket.deadline > now,
                )
                .order_by(ValidatorTicket.issued_at.asc())
                .limit(1)
                .with_for_update()
            )
            if artifact_mode == "screened_only":
                live_ticket_statement = live_ticket_statement.where(
                    Agent.screened_image_sha256.is_not(None),
                    Agent.screened_image_size_bytes.is_not(None),
                    Agent.screened_image_id.is_not(None),
                    Agent.screened_image_ref.is_not(None),
                    Agent.screened_image_upload_id.is_not(None),
                    Agent.screened_image_verified_at.is_not(None),
                )
            if slot_running_benchmark:
                ticket = await session.scalar(live_ticket_statement)
        if ticket is None:
            if rollout is None and source_backfill_rollout is not None:
                # Resume under the helper's canonical slot->row lock order.
                # Restrict this early pass to an existing lease so new source
                # work remains behind active-era ordinary issuance below.
                ticket = await _issue_source_backfill_ticket(
                    session,
                    rollout=source_backfill_rollout,
                    heartbeat=heartbeat,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    active_version=canonical_version,
                    artifact_mode=artifact_mode,
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                    efficiency_config=efficiency_config,
                    slot_settings=slot_settings,
                    resume_only=True,
                    similarity_policy=policy_from_settings(
                        queue_policy.similarity_budget
                    ),
                    similarity_concurrent_submission_limit=(
                        queue_policy.similarity_budget.concurrent_submission_limit
                    ),
                )
            if ticket is None and rollout is None:
                stale_ticket = await session.scalar(
                    select(ValidatorTicket)
                    .where(
                        ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                        ValidatorTicket.slot_id == slot_id,
                        ValidatorTicket.bench_version != canonical_version,
                        ValidatorTicket.status == TicketStatus.ISSUED,
                        ValidatorTicket.deadline > now,
                    )
                    .limit(1)
                    .with_for_update()
                )
                if stale_ticket is not None:
                    if (
                        stale_ticket.purpose != TicketPurpose.CANONICAL_QUORUM
                        or stale_ticket.purpose_revision <= 0
                        or slot_running_benchmark
                    ):
                        # The signed heartbeat says this exact worker is still
                        # occupied, or another authorization lane owns the
                        # lease; leave it untouched and issue nothing else.
                        _record_dispatch_decline(
                            "slot_occupied",
                            validator_hotkey=payload.validator_hotkey,
                            slot_id=slot_id,
                        )
                        return Response(status_code=204)
                    stale_ticket.status = TicketStatus.EXPIRED
                    stale_ticket.deadline = now
                    stale_ticket.retry_after = now
                    await session.flush()
            heartbeat = await session.get(ValidatorHeartbeat, payload.validator_hotkey)
            if ticket is None and rollout is None:
                # Any post-legacy benchmark needs a fresh, identity-matched
                # scorer for THAT version. Keyed on the legacy floor, not on the
                # canary: an activated v3 still gates a v3-incapable validator
                # out once the canary has moved on to v4.
                gated = (inference_required and not target_inference_ready) or (
                    canonical_version > LEGACY_BENCH_VERSION
                    and (
                        heartbeat is None
                        or not heartbeat_supports_version(
                            heartbeat, now=now, version=canonical_version
                        )
                    )
                )
                ticket = (
                    None
                    if gated
                    else await issue_ticket(
                        session,
                        validator_hotkey=payload.validator_hotkey,
                        now=now,
                        ttl=_TICKET_TTL,
                        bench_version=canonical_version,
                        artifact_mode=artifact_mode,
                        validator_running_benchmark=slot_running_benchmark,
                        slot_id=slot_id,
                        efficiency_config=efficiency_config,
                        owner_concurrent_submission_limit=(
                            queue_policy.owner_concurrent_submission_limit
                        ),
                        similarity_policy=policy_from_settings(
                            queue_policy.similarity_budget
                        ),
                        similarity_concurrent_submission_limit=(
                            queue_policy.similarity_budget.concurrent_submission_limit
                        ),
                    )
                )
            if ticket is None and rollout is None:
                # Operator score re-tests are idle-capacity backfill. Ordinary
                # quorum scoring owns the slot whenever it has a candidate.
                ticket = await activate_next_score_retest(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    supports_version=lambda version: (
                        heartbeat is not None
                        and heartbeat_supports_version(
                            heartbeat, now=now, version=version
                        )
                    ),
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                )
            if ticket is None and source_backfill_rollout is not None:
                # Once the inherited top ten is fully established on the new
                # benchmark, an otherwise-idle compatible slot may help settle
                # the retired era. Desired-version cohort, fresh FIFO, retest,
                # and ordinary work all had first claim above. Reusing
                # issue_ticket preserves its bounded 2/3 contender, then 1/3,
                # then 0/3 ordering and every duplicate/owner/slot guard for
                # this low-priority second queue.
                #
                # Only while the source era is still the active one. After
                # activation the retired era is off by default and an operator
                # has to ask for it by name; the helper holds that line.
                ticket = await _issue_source_backfill_ticket(
                    session,
                    rollout=source_backfill_rollout,
                    heartbeat=heartbeat,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    active_version=canonical_version,
                    artifact_mode=artifact_mode,
                    validator_running_benchmark=slot_running_benchmark,
                    slot_id=slot_id,
                    efficiency_config=efficiency_config,
                    slot_settings=slot_settings,
                    carryover_settings=queue_policy.prev_gen_carryover,
                    owner_concurrent_submission_limit=(
                        queue_policy.owner_concurrent_submission_limit
                    ),
                    similarity_policy=policy_from_settings(
                        queue_policy.similarity_budget
                    ),
                    similarity_concurrent_submission_limit=(
                        queue_policy.similarity_budget.concurrent_submission_limit
                    ),
                )
            if ticket is None and rollout is not None:
                # Every issuing lane an open rollout offers has been walked and
                # none had an eligible row: dispatch was willing, the queue was
                # empty for this validator.
                _record_dispatch_decline(
                    "no_candidate",
                    validator_hotkey=payload.validator_hotkey,
                    slot_id=slot_id,
                )
                return Response(status_code=204)
        if ticket is not None:
            agent = await get_agent_by_id(session, agent_id=ticket.agent_id)
            # issue_ticket selected this agent from ``agents``, so it exists.
            assert agent is not None
            dataset = await session.get(
                BenchmarkDataset, (agent.agent_id, ticket.bench_version)
            )
            seed_block = (
                dataset.seed_block if dataset is not None else agent.dataset_seed_block
            )
            seed_block_hash = (
                dataset.seed_block_hash
                if dataset is not None
                else agent.dataset_seed_block_hash
            )
            # Give each of the three quorum validators an independent dataset.
            # The post-commit block hash keeps the seed unpredictable; binding
            # the validator hotkey makes it distinct and publicly reproducible.
            # Persist the pin on the ticket so retries cannot rotate datasets.
            if seed_block_hash is not None and generator.run_size is not None:
                expected_seed = derive_validator_seed(
                    seed_block_hash, agent.agent_id, payload.validator_hotkey
                )
                if ticket.seed is None:
                    ticket.seed = expected_seed
                    ticket.dataset_sha256 = await generator.generate(
                        expected_seed, bench_version=ticket.bench_version
                    )
                    ticket.seed_block = seed_block
                    ticket.seed_block_hash = seed_block_hash
                elif ticket.seed != expected_seed:
                    raise HTTPException(
                        status_code=409,
                        detail="ticket seed does not match its validator identity",
                    )
            contract = benchmark_contract(ticket.bench_version)
            historical_source_ticket = (
                source_backfill_rollout is not None
                and ticket.bench_version == source_backfill_rollout.from_version
            )
            ticket_inference_required = ticket.bench_version >= 7 or (
                not historical_source_ticket
                and (
                    request.app.state.config.inference_proxy.required
                    and ticket.bench_version == canonical_version
                )
            )
            inference_grant = (
                await ensure_inference_grant(
                    session,
                    ticket=ticket,
                    config=inference_config,
                    supported_profiles=(
                        tuple(
                            route.profile_revision
                            for route in v7_calibration.supported_routes
                        )
                        if v7_calibration is not None
                        else None
                    ),
                    calibration_manifest_sha256=(
                        v7_calibration.manifest_sha256
                        if v7_calibration is not None
                        else None
                    ),
                )
                if ticket_inference_required
                else None
            )
            if ticket_inference_required and inference_grant is None:
                raise HTTPException(
                    status_code=503,
                    detail="ticket inference capability is unavailable",
                )
            job = JobResponse(
                agent_id=agent.agent_id,
                slot_id=ticket.slot_id,
                miner_hotkey=agent.miner_hotkey,
                sha256=agent.sha256,
                deadline=ticket.deadline,
                seed=(
                    ticket.seed
                    if ticket.seed is not None
                    else (dataset.seed if dataset is not None else agent.dataset_seed)
                ),
                seed_scope="validator" if ticket.seed is not None else "agent",
                dataset_sha256=(
                    ticket.dataset_sha256
                    if ticket.dataset_sha256 is not None
                    else (
                        dataset.sha256 if dataset is not None else agent.dataset_sha256
                    )
                ),
                run_size=(
                    dataset.run_size if dataset is not None else agent.dataset_run_size
                ),
                dataset_seed_block=ticket.seed_block or seed_block,
                dataset_seed_block_hash=ticket.seed_block_hash or seed_block_hash,
                bench_version=ticket.bench_version,
                minimum_screening_policy_version=(
                    contract.minimum_screening_policy_version
                ),
                requires_screened_image=contract.requires_screened_image,
                benchmark_runtime=(
                    inference_settings.benchmark_runtime
                    if ticket.bench_version >= 10
                    else None
                ),
                inference=(
                    _inference_grant_offer(
                        request=request,
                        grant=inference_grant,
                        bench_version=ticket.bench_version,
                    )
                    if inference_grant is not None
                    else None
                ),
            )
    if job is None:
        # Reached only once every issuing lane has been walked, so this is the
        # candidate walk coming back empty -- not a gate refusing to issue.
        _record_dispatch_decline(
            "no_candidate",
            validator_hotkey=payload.validator_hotkey,
            slot_id=slot_id,
        )
        # Only a fully authenticated, compatible, replay-checked idle poll can
        # trigger bounded convergence. The next poll sees any newly queued work.
        await _refresh_qualification_if_due(
            session,
            generator=generator,
            now=now,
            inference_config=request.app.state.config.inference_proxy,
        )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "issued job agent=%s validator=%s deadline=%s",
        job.agent_id,
        payload.validator_hotkey,
        job.deadline.isoformat(),
    )
    return job


# The only keys this builder reads out of a score's telemetry blob:
# ``_ledger_stderr`` -> ``composite_stderr``, and the legacy paired-confirmation
# pair. Everything else in ``details`` is per-case audit payload -- roughly 22KB
# a row, across every eligible agent, on a path that runs for every continual
# retest request. Selecting the whole blob here put the API worker at 41% of CPU
# inside asyncpg's JSONB decoder and drove /top5-confirmation-job to a measured
# 152s, while the canonical /validator/job path -- which never calls this --
# stayed serviceable. Keep this tuple in step with the readers below.
_KOTH_DETAIL_KEYS = (
    "composite_stderr",
    "confirmation_seeds",
    "confirmation_composites",
)


async def _current_koth_entries(
    session: AsyncSession,
    *,
    canonical_version: int,
    completed_waves_only: bool = True,
    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
    efficiency_config: EfficiencyBonusConfig | None = None,
    now: datetime | None = None,
) -> list[KothEntry]:
    """Build the active-version KOTH fold from canonical or completed evidence.

    Confirmation evidence is admitted only as a complete cohort wave. Partial
    results remain append-only and public for audit, but cannot move the crown
    while sibling leases for the same seed are still running.
    """
    from ditto.api_server.endpoints.scoring import (
        _confirmation_composites,
        _confirmation_seeds,
        _ledger_stderr,
    )

    rows = [
        row
        for row in await list_eligible_ledger(
            session,
            include_fingerprints=False,
            details_keys=_KOTH_DETAIL_KEYS,
            bench_version=canonical_version,
            dedupe_owners=False,
        )
        if row.eligible and row.composite > 0.0
    ]
    rows = rank_submissions(rows)
    quorum = await quorum_composites(
        session,
        [row.agent_id for row in rows],
        bench_versions=dict.fromkeys([row.agent_id for row in rows], canonical_version),
    )
    history = await confirmation_composites_by_seed(
        session,
        agent_ids=[row.agent_id for row in rows],
        bench_version=canonical_version,
    )
    (
        efficiency_bonuses,
        efficiency_factors,
        efficiency_curve_versions,
    ) = await resolve_efficiency_adjustments(
        session,
        rows=rows,
        efficiency_config=efficiency_config,
        now=now,
    )
    raw_scores = {
        row.agent_id: (
            row.v9_confirmation["full_effective_micros"] / 1_000_000
            if row.bench_version == 9 and row.v9_confirmation is not None
            else row.composite
        )
        for row in rows
    }
    raw_rows = dedupe_owner_rows(rows, scores=raw_scores)
    raw_entries = [
        KothEntry(
            miner_hotkey=row.miner_hotkey,
            agent_id=row.agent_id,
            composite=(
                row.v9_confirmation["full_effective_micros"] / 1_000_000
                if row.bench_version == 9 and row.v9_confirmation is not None
                else row.composite
            ),
            first_seen=row.fold_first_seen,
            raw_rank=rank,
            bench_version=row.bench_version,
            composite_stderr=(
                row.v9_confirmation["full_stderr_micros"] / 1_000_000
                if row.bench_version == 9 and row.v9_confirmation is not None
                else _ledger_stderr(
                    row.details if isinstance(row.details, dict) else {},
                    quorum.get(row.agent_id, []),
                )
            ),
            efficiency_bonus=efficiency_bonuses.get(row.agent_id),
            efficiency_factor=efficiency_factors.get(row.agent_id),
            efficiency_curve_version=efficiency_curve_versions.get(row.agent_id),
        )
        for rank, row in enumerate(raw_rows, start=1)
    ]
    raw_members = emission_set(project_koth(raw_entries))
    eligible_seeds = fold_eligible_seeds_by_agent(
        member_ids=[member.agent_id for member in raw_members],
        seeds_by_agent={
            agent_id: values.keys() for agent_id, values in history.items()
        },
        mode=wave_membership,
        anchored_seeds=(
            fold_seed_bound(
                champion_agent_id=raw_members[0].agent_id,
                anchor_version=canonical_version,
                seeds_by_agent={
                    agent_id: values.keys() for agent_id, values in history.items()
                },
            )
            if raw_members
            else None
        ),
    )
    entries: list[KothEntry] = []
    for rank, row in enumerate(rows, start=1):
        details = row.details if isinstance(row.details, dict) else {}
        v9_confirmed = row.bench_version == 9 and row.v9_confirmation is not None
        v9_receipt = row.v9_confirmation if v9_confirmed else None
        merged: dict[int, float] = {}
        legacy_seeds = None if v9_confirmed else _confirmation_seeds(details)
        legacy_composites = None if v9_confirmed else _confirmation_composites(details)
        if legacy_seeds is not None and legacy_composites is not None:
            merged.update(zip(legacy_seeds, legacy_composites, strict=False))
        agent_eligible = eligible_seeds.get(row.agent_id, frozenset())
        if completed_waves_only and not v9_confirmed:
            merged.update(
                {
                    seed: value
                    for seed, value in history.get(row.agent_id, {}).items()
                    if seed in agent_eligible
                }
            )
        elif not v9_confirmed:
            # Compatibility view for leases issued before cohort-wave gating.
            # New KOTH/ledger projections must never use this mode.
            merged.update(history.get(row.agent_id, {}))
        confirmations = tuple(sorted(merged.items())) if len(merged) >= 2 else None
        entries.append(
            KothEntry(
                miner_hotkey=row.miner_hotkey,
                agent_id=row.agent_id,
                composite=(
                    v9_receipt["full_effective_micros"] / 1_000_000
                    if v9_receipt is not None
                    else row.composite
                ),
                first_seen=row.fold_first_seen,
                raw_rank=rank,
                bench_version=row.bench_version,
                composite_stderr=(
                    v9_receipt["full_stderr_micros"] / 1_000_000
                    if v9_receipt is not None
                    else _ledger_stderr(details, quorum.get(row.agent_id, []))
                ),
                quorum_composites=(
                    None
                    if v9_confirmed
                    else tuple(quorum.get(row.agent_id, [])) or None
                ),
                completed_wave_composites=tuple(
                    value
                    for seed, value in sorted(
                        ({} if v9_confirmed else history.get(row.agent_id, {})).items()
                    )
                    if seed in agent_eligible
                )
                or None,
                confirmation_composites=(
                    tuple(value for _seed, value in confirmations)
                    if confirmations is not None
                    else None
                ),
                confirmation_seeds=(
                    tuple(seed for seed, _value in confirmations)
                    if confirmations is not None
                    else None
                ),
                efficiency_bonus=efficiency_bonuses.get(row.agent_id),
                efficiency_factor=efficiency_factors.get(row.agent_id),
                efficiency_curve_version=efficiency_curve_versions.get(row.agent_id),
            )
        )
    quality_primary = any(entry.efficiency_factor is not None for entry in entries)
    entry_scores = {
        entry.agent_id: (
            continual_composite(entry)
            if quality_primary
            else effective_composite(entry)
        )
        for entry in entries
    }
    entry_tiebreaks = (
        {entry.agent_id: effective_composite(entry) for entry in entries}
        if quality_primary
        else None
    )
    selected_rows = dedupe_owner_rows(
        rows, scores=entry_scores, secondary_scores=entry_tiebreaks
    )
    selected_by_id = {row.agent_id: row for row in selected_rows}
    return [
        replace(entry, first_seen=selected_by_id[entry.agent_id].fold_first_seen)
        for entry in entries
        if entry.agent_id in selected_by_id
    ]


async def _current_emission_set(
    session: AsyncSession,
    *,
    canonical_version: int,
    completed_waves_only: bool = True,
    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP,
    efficiency_config: EfficiencyBonusConfig | None = None,
    now: datetime | None = None,
) -> tuple[KothEntry, ...]:
    entries = await _current_koth_entries(
        session,
        canonical_version=canonical_version,
        completed_waves_only=completed_waves_only,
        wave_membership=wave_membership,
        efficiency_config=efficiency_config,
        now=now,
    )
    return emission_set(project_koth(entries))


async def _current_retest_cohort(
    session: AsyncSession,
    *,
    canonical_version: int,
    settings: ContinualRetestSettings,
    efficiency_config: EfficiencyBonusConfig | None = None,
    now: datetime | None = None,
) -> tuple[tuple[KothEntry, ...], tuple[KothEntry, ...], tuple[KothEntry, ...]]:
    """Return ``(emission_set, wave_members, retest_cohort)`` from one read.

    Both are returned because the lane needs them for different jobs and must
    not disagree about the champion: the emission set decides when a wave is
    complete (and therefore which seed is open), the cohort decides who may be
    leased against that seed.

    Only the *cohort* half is widened by the tie band. The emission set is frozen
    consensus shared with the subnet's weight fold and is never five-plus-ties;
    an extended member is extra evidence, never an extra emission recipient.

    ``settings.wave_membership`` has to match what the public board is using,
    because the champion it produces is the seed anchor. Two different answers
    here and on the leaderboard would derive two different seed families, and the
    validator's confirmation would be rejected as un-anchored.

    Wave completion is deliberately keyed to the raw-score emission set inside
    :func:`_current_koth_entries`: those are the members whose shared coverage
    decides which confirmation rows enter the fold. Once the fold moves a raw
    member below rank five, however, the ordinary retest cohort no longer
    includes it. Without returning and serving those raw ``wave_members`` the
    lane deadlocks permanently at that member's depth: the scheduler keeps
    retesting the folded top five while the fold waits for somebody it will
    never schedule.

    The returned retest cohort is therefore the configured folded cohort plus
    every raw wave member, in that order. This may add at most five gate
    catch-up members; it changes neither emissions nor score arithmetic.
    """
    entries = await _current_koth_entries(
        session,
        canonical_version=canonical_version,
        wave_membership=settings.wave_membership,
        efficiency_config=efficiency_config,
        now=now,
    )
    projection = project_koth(entries)
    raw_entries = [
        replace(
            entry,
            quorum_composites=None,
            completed_wave_composites=None,
            confirmation_composites=None,
            confirmation_seeds=None,
        )
        for entry in entries
    ]
    wave_members = emission_set(project_koth(raw_entries))
    statistical = settings.retest_eligibility_mode == "statistical"
    emission_members = emission_set(projection)
    configured_cohort = retest_cohort(
        entries,
        projection,
        size=settings.retest_cohort_size,
        max_size=settings.retest_cohort_max_size if statistical else None,
        tolerance_z=settings.retest_eligibility_z if statistical else 0.0,
    )
    seen = {member.agent_id for member in configured_cohort}
    combined_cohort = (
        *configured_cohort,
        *(member for member in wave_members if member.agent_id not in seen),
    )
    return emission_members, wave_members, combined_cohort


async def _confirm_king_onchain_weights(
    app_state: Any,
    chain: ChainClient,
    session: AsyncSession,
    *,
    now: datetime,
) -> None:
    """Arm any ever-king's public window once the chain confirms its weights.

    Reads the REVEALED weight matrix (post commit-reveal) and stamps
    ``weight_confirmed_at`` for every ever-king miner that now has validator
    weight set on it. Erring toward weights, not realized emission magnitude, so
    a genuine king is never trapped private. Throttled via ``app_state`` so the
    score path reads the chain at most once per interval while a king is pending;
    once no king is unconfirmed, it does zero chain work. The caller wraps this
    best-effort so a chain hiccup never fails an already-committed score.
    """
    last_checked = getattr(app_state, "king_weight_checked_at", None)
    if last_checked is not None and (now - last_checked) < _KING_WEIGHT_CHECK_INTERVAL:
        return
    app_state.king_weight_checked_at = now
    pending = await list_unconfirmed_kings(session)
    # Release the read transaction so the (potentially multi-second) chain call
    # never holds a DB transaction open, and so the write below can open its own.
    await session.rollback()
    if not pending:
        return
    netuid = app_state.config.chain.netuid
    snapshot = await asyncio.wait_for(
        chain.get_weights(netuid), timeout=_KING_WEIGHT_CHECK_TIMEOUT_SECONDS
    )
    weighted_hotkeys = {
        weight.hotkey for vector in snapshot.vectors for weight in vector.weights
    }
    confirmed = [agent_id for agent_id, hotkey in pending if hotkey in weighted_hotkeys]
    if confirmed:
        async with session.begin():
            for agent_id in confirmed:
                await record_weight_confirmed(session, agent_id=agent_id, now=now)


async def _champion_anchored_seed_set(
    session: AsyncSession,
    *,
    canonical_version: int,
    completed_waves_only: bool = True,
) -> frozenset[int]:
    members = await _current_emission_set(
        session,
        canonical_version=canonical_version,
        completed_waves_only=completed_waves_only,
    )
    if not members:
        return frozenset()
    history = await confirmation_composites_by_seed(
        session,
        agent_ids=tuple(member.agent_id for member in members),
        bench_version=canonical_version,
    )
    return frozenset(
        bounded_continual_seed_set(
            members[0].agent_id,
            version=canonical_version,
            composites_by_agent=history,
        )
    )


async def _top5_confirmation_seed_plan(
    session: AsyncSession,
    *,
    champion_agent_id: UUID,
    member_agent_id: UUID,
    wave_member_ids: tuple[UUID, ...],
    cohort_member_ids: tuple[UUID, ...] = (),
    canonical_version: int,
) -> tuple[int, ...]:
    """Every seed this member still owes: its backlog first, then wave growth.

    Which seed is open is decided by the emission set alone, never by the wider
    retest cohort. An extended member that never gets leased (or fails) must not
    be able to hold the wave open, because the open wave is what gates the KOTH
    fold: keying completion to the top five keeps the crown moving at exactly
    the cadence it did before the cohort could be widened. That is *growth*
    pacing only -- ``cohort_member_ids`` widens whose coverage counts as backlog,
    which cannot hold any wave open because backlog is never a wave.

    Two kinds of work come back, and the difference is load-bearing:

    *Catch-up* -- target seeds another cohort member already holds and this one
    does not. Recorded evidence, no wave left to tear, so the bounded backlog is
    returned at once and the fleet can drain it in parallel (one lease per seed;
    see :func:`_claimable_confirmation_seed`). This is what a member promoted
    into the top five at depth zero owes.

    *Growth* -- at most the single next unfinished target seed. The target is
    variance-sized from the emission set and hard-capped, so a completed target
    stops introducing datasets instead of growing forever.
    """
    cohort_ids = tuple(dict.fromkeys((*cohort_member_ids, *wave_member_ids)))
    history = await confirmation_composites_by_seed(
        session,
        agent_ids=tuple(dict.fromkeys((*cohort_ids, member_agent_id))),
        bench_version=canonical_version,
    )
    wave_history = {agent_id: history.get(agent_id, {}) for agent_id in wave_member_ids}
    target_seeds = bounded_continual_seed_set(
        champion_agent_id,
        version=canonical_version,
        composites_by_agent=wave_history,
    )
    seeds_by_agent = {agent_id: values.keys() for agent_id, values in history.items()}
    # Catch-up spans the whole retest cohort, not just the emission set. It used
    # to be scoped to the top five on the grounds that widening it "would
    # multiply retest volume by the cohort size for no convergence benefit" --
    # true while the fold only ever read the emission set's intersection, and no
    # longer true now that every cohort member's coverage is a lease target for
    # every other. The volume argument also assumed the fleet was slot-starved;
    # validators advertise up to eight slots each (ditto-subnet #280) and sit
    # mostly idle, so the backlog drains in parallel rather than queueing.
    catchup = confirmation_catchup_seeds(
        member_id=member_agent_id,
        peer_ids=cohort_ids,
        anchored_seeds=target_seeds,
        seeds_by_agent=seeds_by_agent,
    )
    # Deliberately the STRICT intersection, and not ``wave_membership``. This
    # decides which seed to ISSUE next, not which evidence may be folded. A
    # member at depth zero is exactly the member that still needs leasing, so
    # excluding it here would declare the wave finished and stop the catch-up
    # that gives a new entrant its evidence in the first place. The scoring
    # policy widens what counts; the issuance policy must stay conservative or
    # the two disagree about whether there is work left to do.
    completed = completed_confirmation_wave_seeds(
        member_ids=wave_member_ids,
        seeds_by_agent=seeds_by_agent,
    )
    next_seed = next((seed for seed in target_seeds if seed not in completed), None)
    if (
        next_seed is None
        or next_seed in history.get(member_agent_id, {})
        or next_seed in catchup
    ):
        return catchup
    return (*catchup, next_seed)


async def _live_retest_leases(
    session: AsyncSession,
    *,
    member_agent_ids: Sequence[UUID],
    canonical_version: int,
    now: datetime,
) -> dict[UUID, dict[str, int | None]]:
    """Per member, ``{validator_hotkey: leased seed}`` for open retest leases.

    A ``None`` seed is a pre-protocol-13 bundle lease, which was authorised
    against the whole champion-anchored family rather than one seed; callers
    have to treat it as covering everything the member owes.
    """
    if not member_agent_ids:
        return {}
    rows = await session.execute(
        select(
            ValidatorTicket.agent_id,
            ValidatorTicket.validator_hotkey,
            ValidatorTicket.seed,
        ).where(
            ValidatorTicket.agent_id.in_(list(dict.fromkeys(member_agent_ids))),
            ValidatorTicket.bench_version == canonical_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
            ValidatorTicket.deadline > now,
        )
    )
    leases: dict[UUID, dict[str, int | None]] = {}
    for agent_id, validator_hotkey, seed in rows:
        leases.setdefault(agent_id, {})[validator_hotkey] = seed
    return leases


def _claimable_confirmation_seed(
    *,
    seeds: Sequence[int],
    leases: Mapping[str, int | None],
    validator_hotkey: str,
) -> int | None:
    """Which pending seed this validator may run, or None when all are taken.

    The plan is the member's whole outstanding gap; this hands out one seed of
    it per validator so K validators converge K seeds in the time one used to
    take one. The ticket key is ``(agent, version, validator)``, so distinct
    validators leasing distinct seeds of the same member is already
    representable -- what was missing was the platform ever offering them a
    second seed.

    A validator that already holds a lease on this member gets that same seed
    back. Re-polls and restarts have to be idempotent, and the downstream
    coverage guard recognises its own live ticket only by an exact
    ``(member, version, seed)`` match.
    """
    own = leases.get(validator_hotkey, _MISSING_LEASE)
    if own is not _MISSING_LEASE:
        # A legacy bundle lease named no seed; answer with the head of the plan
        # exactly as the single-seed planner did before it.
        return own if own is not None else (seeds[0] if seeds else None)
    if any(seed is None for seed in leases.values()):
        return None
    claimed = {seed for seed in leases.values() if seed is not None}
    return next((seed for seed in seeds if seed not in claimed), None)


async def _unserved_catchup_members(
    session: AsyncSession,
    *,
    champion_agent_id: UUID,
    emission_member_ids: Sequence[UUID],
    canonical_version: int,
    now: datetime,
) -> frozenset[UUID]:
    """Emission members owing backlog seeds that no live lease is covering.

    "Unserved" rather than merely "behind": a member whose whole backlog is
    already leased out is converging as fast as it can, and letting it keep
    blocking extended-cohort work would idle capacity for nothing.
    """
    members = tuple(dict.fromkeys(emission_member_ids))
    if len(members) < 2:
        return frozenset()
    history = await confirmation_composites_by_seed(
        session, agent_ids=members, bench_version=canonical_version
    )
    target_seeds = bounded_continual_seed_set(
        champion_agent_id,
        version=canonical_version,
        composites_by_agent=history,
    )
    seeds_by_agent = {agent_id: values.keys() for agent_id, values in history.items()}
    leases = await _live_retest_leases(
        session,
        member_agent_ids=members,
        canonical_version=canonical_version,
        now=now,
    )
    unserved: list[UUID] = []
    for member_id in members:
        catchup = confirmation_catchup_seeds(
            member_id=member_id,
            peer_ids=members,
            anchored_seeds=target_seeds,
            seeds_by_agent=seeds_by_agent,
        )
        if not catchup:
            continue
        member_leases = leases.get(member_id, {})
        if any(seed is None for seed in member_leases.values()):
            # A legacy bundle lease covers the whole family; nothing is unserved.
            continue
        covered = {seed for seed in member_leases.values() if seed is not None}
        if any(seed not in covered for seed in catchup):
            unserved.append(member_id)
    return frozenset(unserved)


async def _top5_member_is_least_covered(
    session: AsyncSession,
    *,
    members: tuple[KothEntry, ...],
    emission_member_ids: frozenset[UUID],
    catchup_member_ids: frozenset[UUID],
    requested_member_id: UUID,
    wave_seed: int,
    validator_hotkey: str,
    canonical_version: int,
    now: datetime,
) -> bool:
    """Admit one unclaimed member in the current cohort wave.

    ``members`` is the whole retest cohort; ``emission_member_ids`` is the top
    five inside it. While any emission-set member still needs this seed, only
    emission-set members are admitted: the wave completes on the top five, so
    spending a slot on rank 12 first would delay every crown decision behind it.
    Extended members take the seed once the five are claimed or already scored,
    which is precisely the spare capacity the wider cohort is meant to use.

    ``catchup_member_ids`` extends that same priority one step: emission members
    that still owe backlog seeds nothing else is waiting on. Without it a member
    promoted at depth zero could hold one lease while every other validator
    spent its slot topping up rank 12, and the board would stay on the degraded
    estimator for longer than the backlog actually needs.

    Being "behind" is a fact about stored evidence, not an event: a member is
    privileged exactly while it owes seeds its peers already hold, and stops the
    moment it does not. Confirmation rows are append-only, so a member that
    oscillates in and out of the emission set keeps what it earned and cannot
    re-acquire a backlog by re-entering -- there is no promotion timestamp to
    game. The privilege only ever *declines* another retest claim; it can no
    more reach the ordinary scoring queue than any other predicate here can.
    """
    member_ids = [member.agent_id for member in members]
    if requested_member_id not in member_ids:
        return False

    # A validator may fill several advertised slots with different cohort
    # members. Re-asking for an existing member remains idempotent, but a lease
    # for member A must not veto a free slot for member B. Slot capacity and
    # collision rails are enforced below by ``_idle_retest_slot`` and the
    # unique live-slot index.
    existing_retests = (
        await session.scalars(
            select(ValidatorTicket).where(
                ValidatorTicket.validator_hotkey == validator_hotkey,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                ValidatorTicket.deadline > now,
            )
        )
    ).all()
    if any(
        ticket.agent_id == requested_member_id
        and ticket.bench_version == canonical_version
        and ticket.seed == wave_seed
        for ticket in existing_retests
    ):
        return True

    history = await confirmation_composites_by_seed(
        session,
        agent_ids=member_ids,
        bench_version=canonical_version,
    )
    eligible: list[UUID] = []
    for member_id in member_ids:
        latest_retest = await get_latest_score_retest_event(
            session,
            agent_id=member_id,
            validator_hotkey=validator_hotkey,
        )
        if (
            latest_retest is not None
            and latest_retest.event == EVENT_SCORE_RETEST_REQUESTED
        ):
            continue
        existing_ticket = await session.get(
            ValidatorTicket,
            (member_id, canonical_version, validator_hotkey),
        )
        if existing_ticket is not None and existing_ticket.retry_after is not None:
            retry_after = existing_ticket.retry_after
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            if retry_after > now:
                continue
        if wave_seed not in history.get(member_id, {}):
            eligible.append(member_id)
    if requested_member_id not in eligible:
        return False

    active_rows = await session.execute(
        select(ValidatorTicket.agent_id).where(
            ValidatorTicket.agent_id.in_(eligible),
            ValidatorTicket.bench_version == canonical_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
            ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
            or_(ValidatorTicket.seed == wave_seed, ValidatorTicket.seed.is_(None)),
        )
    )
    claimed = set(active_rows.scalars())
    if requested_member_id in claimed:
        return False
    waiting_emission_members = [
        member_id
        for member_id in eligible
        if member_id in emission_member_ids and member_id not in claimed
    ]
    # Members with an unserved backlog join the waiting set even when they are
    # not waiting on THIS seed -- they may already hold a lease on another one.
    # Ordering only, and only inside this lane: the result of a False here is a
    # 409 on the retest endpoint, never a slot taken from ``request_job``.
    waiting = list(
        dict.fromkeys(
            (
                *waiting_emission_members,
                *(
                    member_id
                    for member_id in member_ids
                    if member_id in catchup_member_ids
                ),
            )
        )
    )
    return not waiting or requested_member_id in waiting


async def _canonical_tail_is_draining(
    session: AsyncSession,
    *,
    requesting_validator: str,
    canonical_version: int,
    now: datetime,
) -> bool:
    """Whether current-version quorum work is finishing or just finished.

    Honest validators ask for canonical work before entering the continual
    top-five lane.  While the last canonical leases are still running, a
    validator that received no job would otherwise sit idle until the next
    scheduled confirmation tempo.  Treat that bounded tail-drain window as
    spare capacity without opening the continual lane permanently between
    tempos.  Keep the window open for one canonical lease TTL after the last
    score lands: otherwise the final score closes the live-ticket predicate
    before the idle validators' next poll, recreating the idle gap this guard
    exists to fill.
    """
    recently_settled_after = now - _TICKET_TTL
    active_agent_id = await session.scalar(
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.bench_version == canonical_version,
            ValidatorTicket.validator_hotkey != requesting_validator,
            ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
            or_(
                (
                    (ValidatorTicket.status == TicketStatus.ISSUED)
                    & (ValidatorTicket.deadline > now)
                ),
                (
                    (ValidatorTicket.status == TicketStatus.SCORED)
                    & (ValidatorTicket.updated_at >= recently_settled_after)
                ),
            ),
        )
        .limit(1)
    )
    return active_agent_id is not None


@router.post(
    "/top5-confirmation-job",
    response_model=JobResponse,
    responses={
        204: {"description": "No authoritative retest is claimable for this slot."},
        401: {"description": "Missing/invalid validator auth."},
        409: {"description": "Stale/replayed claim, closed round, or non-member."},
        426: {"description": "Validator software or protocol must be upgraded."},
        428: {"description": "A fresh signed validator heartbeat is required."},
        503: {"description": "Chain unavailable for the permit / tempo check."},
    },
)
async def request_top5_confirmation_job(
    payload: Top5ConfirmationJobRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    generator: GeneratorDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> JobResponse | Response:
    """Lease one current emission-set member for append-only shared-seed work."""
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError(
            "top-5 confirmation claim header does not match signed hotkey"
        )
    signed = _top5_confirmation_job_signing_message(
        payload.validator_hotkey,
        payload.nonce,
        payload.requested_at,
        slot_id=payload.slot_id,
        champion_agent_id=payload.champion_agent_id,
        member_agent_id=payload.member_agent_id,
    )
    if not _verify_signature(payload.validator_hotkey, signed, payload.signature):
        raise ValidatorAuthError("top-5 confirmation claim signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409, detail="top-5 confirmation claim timestamp is stale"
        )

    config = request.app.state.config
    if config.top5_backoff_base <= 0:
        if payload.slot_id is not None:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        raise HTTPException(
            status_code=409, detail="top-5 shared-seed rescore lane is disabled"
        )
    await _assert_validator_permitted(
        chain,
        config.chain.netuid,
        payload.validator_hotkey,
        network=config.chain.subtensor_network,
    )
    block = await chain.get_latest_block()
    continual_resolver: ContinualRetestSettingsResolver = (
        request.app.state.continual_retest_settings
    )
    continual_settings = await continual_resolver.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    efficiency_config = await request.app.state.efficiency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    if efficiency_config.enabled:
        await ensure_current_efficiency_state(
            request.app.state, session, efficiency_config, now=now
        )
    # Resolved before the transaction opens: the resolver reads on its own
    # session, and this one supplies the request budget stamped onto new grants.
    inference_config = await resolved_proxy_config(
        request.app.state, config.inference_proxy
    )
    inference_settings = await request.app.state.inference_concurrency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    slot_settings = await _validator_slot_settings(request)

    async with session.begin():
        await _assert_validator_compatible(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            config=config.validator_compatibility,
        )
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _JOB_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as exc:
            raise HTTPException(
                status_code=409,
                detail="top-5 confirmation claim nonce has already been used",
            ) from exc
        canonical_version = await active_bench_version(session)
        auto_routed = payload.slot_id is not None
        live_retest: ValidatorTicket | None = None
        if auto_routed:
            live_retest = await session.scalar(
                select(ValidatorTicket)
                .where(
                    ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                    ValidatorTicket.bench_version == canonical_version,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.deadline > now,
                    ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                    ValidatorTicket.slot_id == payload.slot_id,
                )
                .limit(1)
            )
        if validator_issuance_paused(
            slot_settings, validator_hotkey=payload.validator_hotkey
        ):
            # Do not strand a continual lease that was issued before the
            # operator pause. The exact validator/member lease may still be
            # resumed and reported; any new member assignment is withheld.
            if not auto_routed:
                live_retest = await session.scalar(
                    select(ValidatorTicket)
                    .where(
                        ValidatorTicket.agent_id == payload.member_agent_id,
                        ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                        ValidatorTicket.bench_version == canonical_version,
                        ValidatorTicket.status == TicketStatus.ISSUED,
                        ValidatorTicket.deadline > now,
                        ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                    )
                    .limit(1)
                )
            if live_retest is None:
                logger.info(
                    "declined continual retest reason=validator_paused "
                    "validator=%s member=%s",
                    payload.validator_hotkey,
                    payload.member_agent_id,
                )
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
        heartbeat = await session.get(ValidatorHeartbeat, payload.validator_hotkey)
        if heartbeat is None or heartbeat.protocol_version < 13:
            raise HTTPException(
                status_code=428,
                detail=(
                    "a fresh heartbeat with protocol 13 is required for "
                    "single-seed top-five retests"
                ),
            )
        # Yield previous-generation rescoring to an open rollout. This is a
        # stand-down at issuance only: a lease already held elsewhere still runs
        # and reports, so no shared-seed wave is torn in half. It also lifts
        # when the desired version already owns the board -- leftover
        # collecting work is then previous-cohort catchup, not a reason to
        # pause current-generation retests -- and when `open_rollout` itself
        # goes away on activation or supersede. `active_bench_version` and
        # the k=3 quorum are untouched.
        standdown_rollout = await open_rollout(session)
        standdown = rollout_standdown_reason(
            continual_settings,
            open_rollout_desired_version=(
                standdown_rollout.desired_version
                if standdown_rollout is not None
                else None
            ),
            validator_supports_desired_version=(
                standdown_rollout is not None
                and heartbeat_supports_version(
                    heartbeat, now=now, version=standdown_rollout.desired_version
                )
            ),
            desired_authority_earned=(
                standdown_rollout is not None
                and canonical_version == standdown_rollout.desired_version
            ),
        )
        if standdown is not None:
            assert standdown_rollout is not None
            logger.info(
                "continual retest standing down validator=%s active_version=%s "
                "rollout_desired_version=%s mode=%s",
                payload.validator_hotkey,
                canonical_version,
                standdown_rollout.desired_version,
                continual_settings.rollout_standdown,
            )
            if auto_routed and live_retest is None:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            raise HTTPException(status_code=409, detail=standdown)
        v7_calibration = None
        if canonical_version >= 7:
            try:
                capabilities = ValidatorCapabilities.model_validate_json(
                    json.dumps(
                        heartbeat.capabilities if heartbeat is not None else None
                    )
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=428,
                    detail=(
                        f"fresh benchmark v{canonical_version} inference capability "
                        "is required"
                    ),
                ) from exc
            if capabilities.scorer_benchmarks is not None:
                v7_calibration = capabilities.scorer_benchmarks.v7_calibration
            if (
                heartbeat is None
                or heartbeat.protocol_version < 11
                or not capabilities.ticket_inference
                or (canonical_version == 7 and v7_calibration is None)
            ):
                raise HTTPException(
                    status_code=428,
                    detail=(
                        f"fresh benchmark v{canonical_version} inference capability "
                        "is required"
                    ),
                )
        emission_members, wave_members, members = await _current_retest_cohort(
            session,
            canonical_version=canonical_version,
            settings=continual_settings,
            efficiency_config=efficiency_config,
            now=now,
        )
        # These sets answer different questions and must not be conflated:
        #
        # * ``wave_member_ids`` is the raw-score gate retained for compatibility
        #   with already-issued waves.
        # * ``emission_member_ids`` is the current authoritative top five after
        #   completed continual aggregates are folded in.
        #
        # The former decides whether a legacy wave may advance.  Fairness and
        # catch-up priority must use the latter, otherwise a newly promoted
        # current top-five agent is treated like spare-capacity rank-N work and
        # can remain at zero confirmations while folded-out raw members keep
        # accumulating samples.
        wave_member_ids = tuple(member.agent_id for member in wave_members)
        emission_member_ids = frozenset(member.agent_id for member in emission_members)
        if not members:
            if auto_routed:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            raise HTTPException(
                status_code=409,
                detail="the current retest cohort is empty",
            )
        # Platform's current fold is the only routing authority. Legacy v1
        # validators still send the champion/member they observed; v2 sends a
        # slot only and lets this transaction pick from the authoritative cohort.
        champion_agent_id = members[0].agent_id
        if (
            payload.champion_agent_id is not None
            and champion_agent_id != payload.champion_agent_id
        ):
            logger.info(
                "continual retest resolving stale champion validator=%s "
                "claimed=%s authoritative=%s member=%s",
                payload.validator_hotkey,
                payload.champion_agent_id,
                champion_agent_id,
                payload.member_agent_id,
            )
        requested_member_id = (
            live_retest.agent_id if live_retest is not None else payload.member_agent_id
        )
        member_ids = tuple(member.agent_id for member in members)
        if requested_member_id is not None and requested_member_id not in member_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    # The RESOLVED size, not the configured one. Under the tie
                    # band those differ, and a validator told "top 5" while the
                    # cohort is actually 7 cannot tell a stale claim from a
                    # misconfigured board.
                    "the requested agent is not in the current retest cohort "
                    f"(top {len(members)})"
                ),
            )
        champion = await get_agent_by_id(session, agent_id=champion_agent_id)
        assert champion is not None
        crown_block = champion.dataset_seed_block or block.number
        scheduled_round = top5_round_is_due(
            block.number,
            crown_block,
            base=config.top5_backoff_base,
            doubling_k=config.top5_backoff_doubling_tempos,
            cap=config.top5_backoff_cap,
        )
        spare_capacity_round = (
            not scheduled_round
            and await _canonical_tail_is_draining(
                session,
                requesting_validator=payload.validator_hotkey,
                canonical_version=canonical_version,
                now=now,
            )
        )
        # Serialize the coverage read with ticket issuance. Catch-up is allowed
        # outside the reign cadence below, so two validators must not both see
        # the same unserved gap and spend separate slots on it.
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended("top5-confirmation-fairness", 0)
                    )
                )
            )
        catchup_member_ids = await _unserved_catchup_members(
            session,
            champion_agent_id=champion_agent_id,
            emission_member_ids=tuple(member.agent_id for member in emission_members),
            canonical_version=canonical_version,
            now=now,
        )
        candidate_member_ids: tuple[UUID, ...]
        if requested_member_id is not None:
            candidate_member_ids = (requested_member_id,)
        else:
            # Settle the emission wave before spending spare capacity deeper in
            # the configured/statistical cohort. Platform's fairness guard below
            # is still authoritative; this ordering merely avoids rejected probes.
            candidate_member_ids = (
                *(
                    member_id
                    for member_id in member_ids
                    if member_id in emission_member_ids
                ),
                *(
                    member_id
                    for member_id in member_ids
                    if member_id not in emission_member_ids
                ),
            )

        selected_member_id: UUID | None = None
        selected_wave_seed: int | None = None
        legacy_decline: str | None = None
        for candidate_member_id in candidate_member_ids:
            if (
                not scheduled_round
                and not spare_capacity_round
                and not continual_settings.idle_retests_enabled
                and candidate_member_id not in catchup_member_ids
            ):
                legacy_decline = (
                    "top-5 shared-seed rescore round is not due at this block"
                )
                continue
            seeds = await _top5_confirmation_seed_plan(
                session,
                champion_agent_id=champion_agent_id,
                member_agent_id=candidate_member_id,
                wave_member_ids=wave_member_ids,
                cohort_member_ids=member_ids,
                canonical_version=canonical_version,
            )
            if not seeds:
                legacy_decline = (
                    "the requested member has no pending confirmation seeds"
                )
                continue
            member_leases = (
                await _live_retest_leases(
                    session,
                    member_agent_ids=(candidate_member_id,),
                    canonical_version=canonical_version,
                    now=now,
                )
            ).get(candidate_member_id, {})
            claimable = _claimable_confirmation_seed(
                seeds=seeds,
                leases=member_leases,
                validator_hotkey=payload.validator_hotkey,
            )
            if claimable is None and requested_member_id is None:
                continue
            wave_seed = claimable if claimable is not None else seeds[0]
            if not await _top5_member_is_least_covered(
                session,
                members=members,
                emission_member_ids=emission_member_ids,
                catchup_member_ids=catchup_member_ids,
                requested_member_id=candidate_member_id,
                wave_seed=wave_seed,
                validator_hotkey=payload.validator_hotkey,
                canonical_version=canonical_version,
                now=now,
            ):
                legacy_decline = "another cohort member has less confirmation coverage"
                continue
            selected_member_id = candidate_member_id
            selected_wave_seed = wave_seed
            break

        if selected_member_id is None or selected_wave_seed is None:
            if requested_member_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=legacy_decline or "the requested retest is not claimable",
                )
            return Response(status_code=204, headers={"Cache-Control": "no-store"})

        confirmation_datasets: list[ConfirmationDatasetPin] = []
        if canonical_version >= 3:
            if generator.run_size is None:
                raise HTTPException(
                    status_code=503,
                    detail="top-5 confirmation dataset generation is unavailable",
                )
            confirmation_datasets = [
                ConfirmationDatasetPin(
                    seed=selected_wave_seed,
                    dataset_sha256=await generator.generate(
                        selected_wave_seed, bench_version=canonical_version
                    ),
                    run_size=generator.run_size,
                )
            ]
        # Place this retest on a real execution slot. The lane occupies one
        # slot exactly like a canonical lease, so it answers the same two
        # questions the canonical lane already answers: which slots is this
        # validator offering (healthy, not already running), and how many
        # leases has the operator allowed it to hold at once. Neither was asked
        # before: the ticket took the ``slot-0`` column default and the lane was
        # invisible to the operator cap.
        retest_slot = (
            live_retest.slot_id
            if live_retest is not None
            else await _idle_retest_slot(
                session,
                heartbeat=heartbeat,
                slot_settings=slot_settings,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                requested_slot_id=payload.slot_id,
            )
        )
        if retest_slot is None:
            if auto_routed:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            raise HTTPException(
                status_code=409,
                detail="validator has no idle slot for a continual retest",
            )
        ticket = await issue_confirmation_ticket(
            session,
            agent_id=selected_member_id,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            ttl=_TICKET_TTL,
            bench_version=canonical_version,
            seed=(selected_wave_seed if confirmation_datasets else None),
            dataset_sha256=(
                confirmation_datasets[0].dataset_sha256
                if confirmation_datasets
                else None
            ),
            slot_id=retest_slot,
        )
        if ticket is None:
            if auto_routed:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            raise HTTPException(
                status_code=409,
                detail=(
                    "validator has another live assignment or this retest is deferred"
                ),
            )
        agent = await get_agent_by_id(session, agent_id=ticket.agent_id)
        assert agent is not None
        dataset = await session.get(
            BenchmarkDataset, (agent.agent_id, ticket.bench_version)
        )
        contract = benchmark_contract(ticket.bench_version)
        inference_grant = await ensure_inference_grant(
            session,
            ticket=ticket,
            config=inference_config,
            supported_profiles=(
                tuple(
                    route.profile_revision for route in v7_calibration.supported_routes
                )
                if v7_calibration is not None
                else None
            ),
            calibration_manifest_sha256=(
                v7_calibration.manifest_sha256 if v7_calibration is not None else None
            ),
        )
        if ticket.bench_version >= 7 and inference_grant is None:
            raise HTTPException(
                status_code=503,
                detail="ticket inference capability is unavailable",
            )
        job = JobResponse(
            agent_id=agent.agent_id,
            slot_id=ticket.slot_id,
            miner_hotkey=agent.miner_hotkey,
            sha256=agent.sha256,
            deadline=ticket.deadline,
            seed=dataset.seed if dataset is not None else agent.dataset_seed,
            dataset_sha256=(
                dataset.sha256 if dataset is not None else agent.dataset_sha256
            ),
            run_size=(
                dataset.run_size if dataset is not None else agent.dataset_run_size
            ),
            dataset_seed_block=(
                dataset.seed_block if dataset is not None else agent.dataset_seed_block
            ),
            dataset_seed_block_hash=(
                dataset.seed_block_hash
                if dataset is not None
                else agent.dataset_seed_block_hash
            ),
            bench_version=ticket.bench_version,
            minimum_screening_policy_version=(
                contract.minimum_screening_policy_version
            ),
            requires_screened_image=contract.requires_screened_image,
            benchmark_runtime=(
                inference_settings.benchmark_runtime
                if ticket.bench_version >= 10
                else None
            ),
            confirmation_datasets=confirmation_datasets,
            inference=(
                _inference_grant_offer(
                    request=request,
                    grant=inference_grant,
                    bench_version=ticket.bench_version,
                )
                if inference_grant is not None
                else None
            ),
        )
    logger.info(
        "issued top-5 rescore job champion=%s member=%s validator=%s",
        champion_agent_id,
        selected_member_id,
        payload.validator_hotkey,
    )
    return job


@router.post(
    "/agent/{agent_id}/top5-confirmation-score",
    response_model=SubmitScoreResponse,
    responses={
        401: {"description": "Signature did not verify / not a permitted validator."},
        409: {"description": "Lease, benchmark, membership, or seed set changed."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def submit_top5_confirmation_score(
    agent_id: UUID,
    payload: SubmitScoreRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> SubmitScoreResponse:
    """Append shared-seed evidence without replacing the canonical k=3 score."""
    response.headers["Cache-Control"] = "no-store"
    report = payload.report
    if payload.ticket_deadline is None:
        raise HTTPException(status_code=409, detail="confirmation lease is missing")
    seeds = report.confirmation_seeds
    composites = report.confirmation_composites
    if (
        seeds is None
        or composites is None
        or not seeds
        or len(seeds) != len(composites)
        or len(set(seeds)) != len(seeds)
    ):
        raise HTTPException(
            status_code=409,
            detail="confirmation report requires unique aligned seed/composite lists",
        )
    signed = _top5_confirmation_score_signing_message(
        payload.validator_hotkey,
        agent_id,
        payload.ticket_deadline,
        report,
    )
    cost_evidence_bound = _verify_signature(
        payload.validator_hotkey, signed, payload.signature
    )
    if not cost_evidence_bound:
        legacy_signed = _top5_confirmation_score_signing_message(
            payload.validator_hotkey,
            agent_id,
            payload.ticket_deadline,
            report,
            bind_base_evidence=False,
        )
        if not _verify_signature(
            payload.validator_hotkey, legacy_signed, payload.signature
        ):
            raise ValidatorAuthError(
                "top-5 confirmation score signature did not verify"
            )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    now = datetime.now(UTC)
    async with session.begin():
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        if agent.status not in {AgentStatus.SCORED, AgentStatus.LIVE}:
            raise HTTPException(
                status_code=409,
                detail="top-5 confirmation target is not finalized and eligible",
            )
        v9_base = _reported_v9_base_evidence(report)
        if v9_base is not None and v9_base.artifact_sha256 != agent.sha256:
            raise HTTPException(
                status_code=409,
                detail="v9 confirmation evidence artifact digest does not match agent",
            )
        canonical_version = await active_bench_version(session)
        if report.bench_version != canonical_version:
            raise HTTPException(
                status_code=409,
                detail="confirmation benchmark version is no longer active",
            )
        ticket = await get_open_ticket(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            deadline=payload.ticket_deadline,
            bench_version=canonical_version,
            for_update=True,
        )
        if ticket is None:
            raise HTTPException(
                status_code=409, detail="confirmation lease is not open"
            )
        legacy_completion = (
            ticket.purpose == TicketPurpose.LEGACY_UNCLASSIFIED
            and ticket.purpose_revision == 0
            and ticket.legacy_completion_allowed
        )
        if not legacy_completion and (
            ticket.purpose != TicketPurpose.CONTINUAL_RETEST
            or ticket.purpose_revision <= 0
        ):
            raise HTTPException(
                status_code=409,
                detail="ticket is not authorized for continual retesting",
            )
        if legacy_completion:
            ticket.purpose = TicketPurpose.CONTINUAL_RETEST
            ticket.purpose_revision = 1
            ticket.legacy_completion_allowed = False
        if ticket.seed is not None:
            if seeds != [ticket.seed] or report.seed != ticket.seed:
                raise HTTPException(
                    status_code=409,
                    detail="confirmation report does not match the leased wave seed",
                )
            if (
                supports_confirmation(canonical_version)
                and cost_evidence_bound
                and ticket.dataset_sha256 is not None
                and _reported_dataset_sha256(report) != ticket.dataset_sha256
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "confirmation report dataset digest does not match the "
                        "leased wave"
                    ),
                )
        else:
            # Bounded compatibility for already-issued bundle leases. New
            # protocol-13 tickets are pinned to exactly one seed above and do
            # not become invalid merely because rollout filtering changed the
            # projected champion while this old run was in flight. The live
            # ticket is the membership authorization; seeds remain bounded to
            # either the completed-wave or legacy partial-wave champion, plus
            # a seed already accepted from a sibling in that same old wave.
            allowed = set(
                await _champion_anchored_seed_set(
                    session, canonical_version=canonical_version
                )
            )
            allowed.update(
                await _champion_anchored_seed_set(
                    session,
                    canonical_version=canonical_version,
                    completed_waves_only=False,
                )
            )
            allowed.update(
                (
                    await session.scalars(
                        select(ConfirmationScore.seed).where(
                            ConfirmationScore.bench_version == canonical_version
                        )
                    )
                ).all()
            )
            if any(seed not in allowed for seed in seeds):
                raise HTTPException(
                    status_code=409,
                    detail="confirmation report contains a non-canonical seed",
                )
        v9_efficiency_token_total: int | None = None
        v9_efficiency_cost_eligible: bool | None = None
        if (
            supports_confirmation(canonical_version)
            and cost_evidence_bound
            and ticket.seed is not None
            and seeds == [ticket.seed]
        ):
            v9_efficiency_token_total = audited_v9_run_token_total(
                report.details,
                base_evidence_sha256=report.base_evidence_sha256,
            )
            v9_efficiency_cost_eligible = v9_efficiency_token_total is not None
        await append_confirmation_scores(
            session,
            rows=[
                ConfirmationSeedScore(
                    agent_id=agent_id,
                    validator_hotkey=payload.validator_hotkey,
                    seed=seed,
                    composite=composite,
                    run_id=report.run_id,
                    signature=payload.signature,
                    v9_efficiency_token_total=v9_efficiency_token_total,
                    v9_efficiency_cost_eligible=v9_efficiency_cost_eligible,
                )
                for seed, composite in zip(seeds, composites, strict=True)
            ],
            bench_version=canonical_version,
            created_at=now,
        )
        await mark_ticket_scored(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            bench_version=canonical_version,
        )
    return SubmitScoreResponse(agent_id=agent_id, status=agent.status, accepted=True)


@router.post(
    "/job/fail",
    response_model=FailJobResponse,
    responses={
        401: {"description": "Missing/invalid validator auth or signature."},
        409: {"description": "Stale or replayed signed fail request."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def fail_job(
    payload: FailJobRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> FailJobResponse:
    """Resolve a failed but still-leased ticket with reason-specific retry policy.

    A validator whose scoring attempt failed calls this so the platform closes
    the live ticket now instead of leaving the lease idle until its own deadline.
    Canonical scoring errors are immediately eligible for another bounded
    attempt; infrastructure, sandbox OOM, and continual-retest failures apply
    their dedicated cooldowns. Any later issue mints a **fresh** lease rather
    than resuming the failed one. Additive and best-effort: an old validator
    that never calls this behaves exactly as today (the ticket expires on its
    own via the overdue sweep).

    Auth mirrors the job claim: the header must match the signed hotkey, the
    signature proves possession, ``requested_at`` is freshness-bounded, the
    nonce is consumed once, and the caller must actually hold the live ticket
    named by ``(agent_id, ticket_deadline)``.
    """
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError("job-fail header does not match signed hotkey")
    signed = _job_fail_signing_message(
        payload.validator_hotkey,
        payload.agent_id,
        payload.ticket_deadline,
        payload.nonce,
        payload.requested_at,
    )
    if not _verify_signature(payload.validator_hotkey, signed, payload.signature):
        raise ValidatorAuthError(
            f"job-fail signature did not verify for hotkey {payload.validator_hotkey}"
        )
    now = datetime.now(UTC)
    requested_at = payload.requested_at.astimezone(UTC)
    if abs(now - requested_at) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(status_code=409, detail="job-fail timestamp is stale")

    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, payload.validator_hotkey, network=network
    )

    reopened = False
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _JOB_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as exc:
            raise HTTPException(
                status_code=409, detail="job-fail nonce has already been used"
            ) from exc
        # Authorize off the live ticket the caller holds (cross-version lookup on
        # the exact lease, same as the heartbeat progress path), never a
        # standalone nonce grant. A missing/expired/spent lease is a safe no-op.
        ticket = await get_open_ticket(
            session,
            agent_id=payload.agent_id,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            deadline=payload.ticket_deadline,
            bench_version=None,
            for_update=True,
        )
        if ticket is not None:
            # Ask the platform's own records whether it killed this lease's
            # inference before believing the reported reason. A validator can
            # only classify from what its scorer told it, and that report has
            # been demonstrably wrong: a run the platform revoked mid-lease
            # reached fail_job as "scoring_error" and spent a real attempt.
            # Checked BEFORE revoke_ticket_inference below, which would
            # otherwise manufacture the very evidence being read, and scoped to
            # this lease's own deadline (captured before the rewrite two lines
            # down) so a prior attempt's cleanup cannot be mistaken for it.
            platform_revoked = await ticket_inference_revoked_mid_lease(
                session,
                agent_id=ticket.agent_id,
                bench_version=ticket.bench_version,
                validator_hotkey=payload.validator_hotkey,
                ticket_deadline=payload.ticket_deadline,
            )
            # Read before the ticket is mutated below, so the total is the
            # fleet's spend on this agent *before* this failure is priced in.
            fleet_infra_grants = await agent_infra_retry_grants(
                session,
                agent_id=ticket.agent_id,
                bench_version=ticket.bench_version,
            )
            # Close for reissue without the 6h agent-failure cooldown so the
            # next request_job mints a fresh lease instead of resuming this one.
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = now
            ticket.failure_reason = payload.reason
            ticket.failure_detail = payload.failure_detail
            ticket.failed_at = now
            if payload.reason == "infrastructure" or platform_revoked:
                # Not the agent's fault: bump the (bounded) infra grant that
                # offsets the coming attempt_count++, so an outage never spends
                # the agent's genuine per-version budget. Then apply an
                # escalating cooldown so a *sustained* outage isn't hammered by
                # immediate back-to-back re-leases of the same agent.
                #
                # The same compensation covers a platform-revoked lease however
                # the validator labelled it. The grant is still bounded, so a
                # persistently broken lease cannot mint attempts forever, and
                # failure_reason still records what was actually reported --
                # this corrects the billing, not the diagnosis.
                #
                # A *reported* infrastructure verdict additionally answers to the
                # per-agent bound: eight grants per validator across a validator
                # pool of any size is unbounded per agent, which is how one
                # artifact held quorum slots for a day in ditto-subnet#279. A
                # platform-revoked lease deliberately does not (see
                # grant_no_fault_retry): repetition there is the platform's fault,
                # and billing the miner for it is the rule #460/#497 settled.
                granted = grant_no_fault_retry(
                    ticket,
                    agent_infra_grants=(
                        fleet_infra_grants
                        if payload.reason == "infrastructure"
                        else None
                    ),
                )
                # Refused means the next lease is billed to the miner. Cool all
                # the way down rather than off this ticket's own (possibly small)
                # count: the fleet has already spent its whole no-fault allowance
                # on this artifact, so nothing about it deserves a fast retry.
                ticket.retry_after = now + (
                    infra_retry_backoff(ticket.infra_retry_grants)
                    if granted
                    else INFRA_RETRY_BACKOFF_CAP
                )
                if not granted:
                    logger.warning(
                        "no-fault retry refused; this failure bills the miner "
                        "agent=%s validator=%s reason=%s detail=%s "
                        "ticket_grants=%s fleet_grants=%s",
                        payload.agent_id,
                        payload.validator_hotkey,
                        payload.reason,
                        payload.failure_detail,
                        ticket.infra_retry_grants,
                        fleet_infra_grants,
                    )
                if platform_revoked and payload.reason != "infrastructure":
                    logger.warning(
                        "platform-revoked lease reported as %s; compensating "
                        "agent=%s validator=%s deadline=%s",
                        payload.reason,
                        payload.agent_id,
                        payload.validator_hotkey,
                        payload.ticket_deadline.isoformat(),
                    )
            elif payload.reason == "sandbox_oom":
                # The sandbox, rather than validator-owned infrastructure,
                # exhausted its memory allowance. Preserve the failed attempt
                # and defer this artifact so the validator immediately advances
                # to another eligible harness instead of reclaiming it.
                ticket.retry_after = now + RETRY_COOLDOWN
            elif ticket.purpose == TicketPurpose.CONTINUAL_RETEST:
                # Canonical scoring errors are bounded by the validator's
                # attempt budget, so another validator may retry immediately.
                # Continual retests deliberately reuse the same mutable ticket
                # beyond that budget; treating their scoring errors the same
                # way creates an unbounded hot loop for one validator/agent
                # pair. Cool only this pair down. Other validators remain free
                # to produce the shared confirmation seed.
                ticket.retry_after = now + RETRY_COOLDOWN
            else:
                # A scoring_error is the agent's own failure: consume the budget
                # and reissue immediately for another validator/attempt.
                ticket.retry_after = now
            await session.flush()
            await revoke_ticket_inference(session, ticket=ticket, now=now)
            reopened = True
    logger.info(
        "validator=%s reported job failure agent=%s reason=%s reopened=%s",
        payload.validator_hotkey,
        payload.agent_id,
        payload.reason,
        reopened,
    )
    return FailJobResponse(agent_id=payload.agent_id, reopened=reopened)


def _stable_version(value: str) -> tuple[int, int, int] | None:
    """Parse the stable release format validators publish in heartbeats."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


async def _assert_validator_compatible(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    config: ValidatorCompatibilityConfig,
) -> None:
    """Reject scoring work until a fresh, supported heartbeat is observed."""
    if config.minimum_software_version is None:
        return
    heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
    if heartbeat is None:
        raise HTTPException(
            status_code=428,
            detail=(
                "validator heartbeat required before requesting work; "
                "update and restart ditto-subnet"
            ),
        )
    seen_at = heartbeat.seen_at
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    if now - seen_at > timedelta(seconds=config.heartbeat_max_age_seconds):
        raise HTTPException(
            status_code=428,
            detail=(
                "validator heartbeat is stale; confirm the current validator "
                "release is running before requesting work"
            ),
        )
    if heartbeat.protocol_version < config.minimum_protocol_version:
        raise HTTPException(
            status_code=426,
            detail=(
                f"validator protocol {heartbeat.protocol_version} is below required "
                f"{config.minimum_protocol_version}; update ditto-subnet"
            ),
        )
    current = _stable_version(heartbeat.software_version)
    minimum = _stable_version(config.minimum_software_version)
    assert minimum is not None  # validated at process boot
    if current is None or current < minimum:
        raise HTTPException(
            status_code=426,
            detail=(
                f"validator software {heartbeat.software_version!r} is below required "
                f"{config.minimum_software_version}; update ditto-subnet"
            ),
        )


async def _validator_artifact_routing(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    heartbeat_max_age_seconds: int,
) -> tuple[Literal["legacy", "prefer_screened", "screened_only"], str | None]:
    """Return signed routing mode/state; pre-v7 reporters remain legacy."""
    heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
    if heartbeat is None or heartbeat.protocol_version < 7:
        return "legacy", None
    seen_at = heartbeat.seen_at
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    if now - seen_at > timedelta(seconds=heartbeat_max_age_seconds):
        raise HTTPException(
            status_code=428,
            detail="validator heartbeat v7 is stale; report a fresh heartbeat",
        )
    try:
        capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(heartbeat.capabilities)
        )
        stack = ValidatorStackIdentity.model_validate_json(json.dumps(heartbeat.stack))
    except ValidationError as error:
        raise HTTPException(
            status_code=428,
            detail=(
                "validator heartbeat v7 capabilities are malformed; "
                "report a fresh heartbeat"
            ),
        ) from error
    if capabilities.full_stack_managed != (stack.mode == "managed"):
        raise HTTPException(
            status_code=428,
            detail="validator heartbeat v7 capabilities contradict stack identity",
        )
    return validator_artifact_mode(capabilities), heartbeat.state


@router.get(
    "/agent/{agent_id}/artifact",
    response_model=ArtifactResponse,
    responses={
        401: {"description": "Missing/invalid validator auth."},
        404: {"description": "No agent with the given id."},
        409: {"description": "No open scoring ticket for this validator/agent."},
        422: {"description": "Malformed UUID path parameter."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def agent_artifact(
    agent_id: UUID,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    storage: StorageDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
    x_validator_artifact_nonce: Annotated[UUID | None, Header()] = None,
    x_validator_artifact_requested_at: Annotated[datetime | None, Header()] = None,
    x_validator_artifact_signature: Annotated[str | None, Header()] = None,
) -> ArtifactResponse:
    """Return an artifact URL after fresh proof of validator-key possession.

    Download is bound to an unexpired ``ISSUED`` scoring ticket for this
    validator and agent. Possession alone is not enough to bulk-fetch competitor
    source or screened images.
    """
    response.headers["Cache-Control"] = "no-store"
    if (
        x_validator_hotkey is None
        or not re.fullmatch(_SS58_PATTERN, x_validator_hotkey)
        or x_validator_artifact_nonce is None
        or x_validator_artifact_requested_at is None
        or x_validator_artifact_signature is None
    ):
        raise ValidatorAuthError("artifact request proof is missing or malformed")
    if x_validator_artifact_requested_at.tzinfo is None:
        raise ValidatorAuthError("artifact request timestamp must include a timezone")
    signed = _artifact_signing_message(
        x_validator_hotkey,
        agent_id,
        x_validator_artifact_nonce,
        x_validator_artifact_requested_at,
    )
    if not _verify_signature(
        x_validator_hotkey, signed, x_validator_artifact_signature
    ):
        raise ValidatorAuthError("artifact request signature did not verify")
    now = datetime.now(UTC)
    if (
        abs(now - x_validator_artifact_requested_at.astimezone(UTC))
        > _JOB_REQUEST_MAX_AGE
    ):
        raise HTTPException(
            status_code=409, detail="artifact request timestamp is stale"
        )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        x_validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=x_validator_artifact_nonce,
                validator_hotkey=x_validator_hotkey,
                now=now,
                expires_at=now + _JOB_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as exc:
            raise HTTPException(
                status_code=409,
                detail="artifact request nonce has already been used",
            ) from exc
        agent = await get_agent_by_id(session, agent_id=agent_id)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        ticket = await session.scalar(
            select(ValidatorTicket).where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.validator_hotkey == x_validator_hotkey,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
        )
        if ticket is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no open scoring ticket for this validator and agent "
                    "(never issued, expired, or already scored)"
                ),
            )
    url = await storage.presigned_get_url(
        key=_artifact_key(agent_id),
        expires_in=int(_ARTIFACT_URL_TTL.total_seconds()),
    )
    image_url = None
    if (
        agent.screened_image_sha256 is not None
        and agent.screened_image_upload_id is not None
    ):
        image_url = await storage.presigned_get_url(
            key=_screened_image_key(agent_id, agent.screened_image_upload_id),
            expires_in=int(_ARTIFACT_URL_TTL.total_seconds()),
        )
    logger.info(
        "validator=%s fetched artifact url for agent_id=%s bench_version=%s",
        x_validator_hotkey,
        agent_id,
        ticket.bench_version,
    )
    # Durable counterpart to the log line above. The ticket is keyed by
    # (agent_id, validator_hotkey), so those two columns plus bench_version
    # identify the lease that authorized this fetch.
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_VALIDATOR_ARTIFACT,
        requester_kind="validator",
        requester_id=x_validator_hotkey,
        bench_version=ticket.bench_version,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(
            request,
            served_screened_image=image_url is not None,
            # The ticket row is UPSERTed on reissue, so attempt_count is the
            # only thing tying this fetch to which lease attempt it served.
            attempt_count=ticket.attempt_count,
            slot_id=ticket.slot_id,
        ),
    )
    return ArtifactResponse(
        agent_id=agent_id,
        sha256=agent.sha256,
        download_url=url,
        expires_at=datetime.now(UTC) + _ARTIFACT_URL_TTL,
        screened_image_url=image_url,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_size_bytes=agent.screened_image_size_bytes,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        bench_version=ticket.bench_version,
        screening_policy_version=agent.screening_policy_version,
    )


@router.post(
    "/agent/{agent_id}/score",
    response_model=SubmitScoreResponse,
    responses={
        401: {"description": "Signature did not verify / not a permitted validator."},
        404: {"description": "No agent with the given id."},
        409: {"description": "Agent is not in a scoreable state."},
        422: {"description": "Malformed request body or UUID path parameter."},
        503: {"description": "Chain unavailable for the permit check."},
    },
)
async def submit_score(
    agent_id: UUID,
    payload: SubmitScoreRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    storage: StorageDep,
    generator: GeneratorDep,
) -> SubmitScoreResponse:
    """Record a DittoBench score report and advance the agent's lifecycle.

    Ordering is cheap-before-expensive and no DB write happens until every
    check passes: (1) signature over ``{validator_hotkey}:{run_id}``,
    (2) on-chain validator-permit check, (3) one transaction that upserts
    the score and, once the k=3 quorum has reported, finalizes the agent
    ``evaluating -> scored`` on the median composite. Below quorum the score
    is recorded and the agent stays provisional (``evaluating``).
    """
    response.headers["Cache-Control"] = "no-store"
    report = payload.report
    v9_base = _reported_v9_base_evidence(report)

    # 1. Signature proves the reporting validator owns the hotkey and binds the
    #    agent + score contents (anti-replay / anti-tamper). CPU-only, no I/O.
    signed = _score_signing_message(
        payload.validator_hotkey, agent_id, payload.ticket_deadline, report
    )
    if not _verify_signature(payload.validator_hotkey, signed, payload.signature):
        raise ValidatorAuthError(
            f"score signature did not verify for hotkey {payload.validator_hotkey}"
        )

    # 2. The hotkey must be a permitted validator on this subnet.
    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, payload.validator_hotkey, network=network
    )

    # 3. Retired-era floor, checked before the transaction opens.
    #
    # Deliberately outside ``session.begin()``: the answer depends on nothing
    # in the database, so taking the agent row lock first would serialize live
    # v7 scorers behind a submission that is going to be refused regardless.
    #
    # The in-flight lease this refuses is left ISSUED rather than closed here.
    # That is the safe direction. Closing it would need this check to live
    # inside the transaction that the rejection then rolls back, and a lease
    # left alone still reaches a terminal state two ways: the validator reports
    # the 410 through ``fail_job`` (``scoring_error`` -- consumes the attempt,
    # mints no grant), or, if it says nothing at all, the overdue sweep expires
    # it at its own deadline, at most ~90 minutes out. Neither can be re-leased
    # afterwards: ``_issue_source_backfill_ticket`` refuses to resume beneath
    # the floor, and the ``validator_tickets_bench_version_floor`` trigger
    # refuses to insert a fresh sub-v7 ticket even if something asked.
    report_version = report.bench_version or LEGACY_BENCH_VERSION
    if report_version < MIN_SCOREABLE_BENCH_VERSION:
        raise RetiredBenchVersionError(
            f"benchmark v{report_version} is retired; the score ledger accepts "
            f"v{MIN_SCOREABLE_BENCH_VERSION} and later only"
        )

    queue_policy = await _resolve_queue_policy(request)

    # 4. Atomic: record the score + advance status together. The row lock
    #    serializes concurrent scorers so the status guard + transition below
    #    can't be lost-updated.
    async with session.begin():
        agent = await get_agent_by_id(
            session, agent_id=agent_id, for_update=True, include_anticopy=True
        )
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        if v9_base is not None and v9_base.artifact_sha256 != agent.sha256:
            raise HTTPException(
                status_code=409,
                detail="v9 base evidence artifact digest does not match the agent",
            )
        if payload.ticket_deadline is None:
            raise HTTPException(
                status_code=409,
                detail="score submission is missing its ticket lease deadline",
            )
        prior_ticket = await session.get(
            ValidatorTicket,
            (agent_id, report_version, payload.validator_hotkey),
            with_for_update=True,
        )
        if prior_ticket is not None and prior_ticket.status == TicketStatus.SCORED:
            prior_score = await session.get(
                Score, (agent_id, report_version, payload.validator_hotkey)
            )
            retry_details = _score_details(
                report,
                ticket_deadline=payload.ticket_deadline,
                bench_version=report_version,
            )
            exact_retry = (
                _lease_token(prior_ticket.deadline)
                == _lease_token(payload.ticket_deadline)
                and prior_score is not None
                and prior_score.run_id == report.run_id
                and prior_score.seed == report.seed
                and prior_score.composite == report.composite
                and prior_score.tool_mean == report.tool_mean
                and prior_score.memory_mean == report.memory_mean
                and prior_score.median_ms == report.median_ms
                and prior_score.n == report.n
                and _aware_utc(prior_score.generated_at)
                == _aware_utc(report.generated_at)
                and _retry_details_match(prior_score.details, retry_details)
            )
            if exact_retry:
                return SubmitScoreResponse(
                    agent_id=agent_id, status=agent.status, accepted=True
                )
            raise HTTPException(
                status_code=409,
                detail="scoring ticket was already consumed by a different result",
            )
        if agent.status not in _SCOREABLE_STATUSES:
            raise AgentNotEvaluatableError(
                f"agent {agent_id} is {agent.status}, not in {_SCOREABLE_STATUSES}"
            )
        if agent.screening_policy_version < SCREENING_POLICY_VERSION:
            raise AgentNotEvaluatableError(
                f"agent {agent_id} has not passed screening policy "
                f"{SCREENING_POLICY_VERSION}"
            )
        # k=3 gate: a score is only accepted against a live ticket this validator
        # holds for the agent. No ticket (never issued, expired, or already
        # spent) means the score is unsolicited or late, so it is rejected and
        # the slot is left for a validator that will score in time. One ticket,
        # one score: the ticket is consumed below, so a re-score needs a new one.
        ticket = await get_open_ticket(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            now=datetime.now(UTC),
            deadline=payload.ticket_deadline,
            # A validator on the old protocol omits bench_version. Falling back
            # to CURRENT would send every legacy submission hunting a ticket
            # for whatever version is current, find none, and 409. A version-less
            # report means v2 by definition, so pin the frozen legacy version --
            # NOT the rollout's from_version, which moves.
            bench_version=report_version,
            for_update=True,
        )
        if ticket is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no open scoring ticket for this validator and agent "
                    "(never issued, expired, or already scored)"
                ),
            )
        legacy_completion = (
            ticket.purpose == TicketPurpose.LEGACY_UNCLASSIFIED
            and ticket.purpose_revision == 0
            and ticket.legacy_completion_allowed
        )
        if not legacy_completion and (
            ticket.purpose != TicketPurpose.CANONICAL_QUORUM
            or ticket.purpose_revision <= 0
        ):
            raise HTTPException(
                status_code=409,
                detail="ticket is not authorized for canonical scoring",
            )
        if legacy_completion:
            ticket.purpose = TicketPurpose.CANONICAL_QUORUM
            ticket.purpose_revision = 1
            ticket.legacy_completion_allowed = False
        # Every post-legacy benchmark must be bound EXPLICITLY, not just the
        # current canary: a v3 ticket keeps this requirement after the canary
        # moves to v4, instead of silently falling through to the lenient branch.
        if ticket.bench_version > LEGACY_BENCH_VERSION:
            if report.bench_version != ticket.bench_version:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"benchmark v{ticket.bench_version} score must explicitly "
                        f"bind bench_version={ticket.bench_version}"
                    ),
                )
        elif report.bench_version not in (None, ticket.bench_version):
            raise HTTPException(
                status_code=409,
                detail="score benchmark version does not match its ticket lease",
            )
        if ticket.seed is not None and report.seed != ticket.seed:
            raise HTTPException(
                status_code=409,
                detail="score seed does not match its validator ticket",
            )
        if (
            ticket.dataset_sha256 is not None
            and _reported_dataset_sha256(report) != ticket.dataset_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="score dataset digest does not match its validator ticket",
            )
        existing_score = await session.get(
            Score, (agent_id, ticket.bench_version, payload.validator_hotkey)
        )
        replacement_event = None
        if existing_score is not None:
            latest_retest = await get_latest_score_retest_event(
                session,
                agent_id=agent_id,
                validator_hotkey=payload.validator_hotkey,
            )
            if (
                latest_retest is None
                or latest_retest.event != EVENT_SCORE_RETEST_REQUESTED
            ):
                if agent.status not in {AgentStatus.SCORED, AgentStatus.LIVE}:
                    latest_retest = None
                else:
                    raise HTTPException(
                        status_code=409,
                        detail="accepted score has no operator-authorized re-test",
                    )
            if latest_retest is None:
                replacement_event = None
            else:
                if (
                    int(latest_retest.payload.get("bench_version", -1))
                    != ticket.bench_version
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="replacement request benchmark version changed",
                    )
                if latest_retest.payload.get("run_id") != existing_score.run_id:
                    raise HTTPException(
                        status_code=409,
                        detail="accepted score changed after replacement request",
                    )
                replacement_event = latest_retest
        # Persist the scoring engine's opaque telemetry (models used,
        # bench_version, dataset_sha256, per-category means, token spend, …) plus
        # the per-case breakdown, all under scores.details. The public leaderboard
        # surfaces a safe subset of this; the full blob (incl. per_case answer-key
        # fields) is only ever read back through validator-gated endpoints.
        score_details = _score_details(
            report,
            ticket_deadline=payload.ticket_deadline,
            bench_version=ticket.bench_version,
        )
        audit_now = datetime.now(UTC)
        if replacement_event is not None and agent.status in {
            AgentStatus.SCORED,
            AgentStatus.LIVE,
        }:
            assert existing_score is not None
            await append_audit_entry(
                session,
                agent_id=agent_id,
                validator_hotkey=payload.validator_hotkey,
                event=EVENT_SCORE_INVALIDATED,
                payload={
                    "request_id": replacement_event.payload["request_id"],
                    "actor": replacement_event.payload["actor"],
                    "reason": replacement_event.payload["reason"],
                    "bench_version": ticket.bench_version,
                    "run_id": existing_score.run_id,
                    "invalidated_score": {
                        "run_id": existing_score.run_id,
                        "seed": existing_score.seed,
                        "composite": existing_score.composite,
                        "tool_mean": existing_score.tool_mean,
                        "memory_mean": existing_score.memory_mean,
                        "median_ms": existing_score.median_ms,
                        "n": existing_score.n,
                        "bench_version": existing_score.bench_version,
                        "ticket_deadline": (
                            existing_score.details.get("ticket_deadline")
                            if isinstance(existing_score.details, dict)
                            else None
                        ),
                        "base_evidence_sha256": (
                            existing_score.details.get("base_evidence_sha256")
                            if isinstance(existing_score.details, dict)
                            else None
                        ),
                        "signature": existing_score.signature,
                        "generated_at": existing_score.generated_at.isoformat(),
                    },
                    "replacement_run_id": report.run_id,
                    "replacement_composite": report.composite,
                },
                recorded_at=audit_now,
            )
        # What this run actually spent on the reader model, read off the grant
        # bound to this exact lease. Captured here, before the grant is revoked
        # a few hundred lines below, and denormalized onto the score because
        # inference_grants is pruned within days while scores are permanent.
        #
        # The validator does not and cannot report these numbers -- the
        # platform's own proxy meters them as it charges each request -- so
        # they are not part of the signed report and need no wire change.
        model_usage = await get_lease_model_usage(session, ticket=ticket)
        # The verdict is stamped alongside the raw counters so a miner can see
        # not just the numbers but the finding and its reason. In SHADOW (the
        # default) this records what enforcement *would* have done and changes
        # no score -- ditto-platform#506 invariant 5. For v9 this remains a
        # platform-owned reconciliation annotation only: the typed, signed
        # ``v9_base.score_gates`` evidence is authoritative and this legacy
        # annotation must never apply a second factor.
        model_use = evaluate_model_use(
            model_usage, cases=report.n, policy=model_use_policy()
        )
        model_use_key = (
            "platform_model_use_reconciliation"
            if ticket.bench_version >= 9
            else "model_use"
        )
        score_details[model_use_key] = model_use.as_public_dict()
        await upsert_score(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            bench_version=ticket.bench_version,
            run_id=report.run_id,
            seed=report.seed,
            composite=report.composite,
            tool_mean=report.tool_mean,
            memory_mean=report.memory_mean,
            median_ms=report.median_ms,
            n=report.n,
            generated_at=report.generated_at,
            signature=payload.signature,
            details=score_details or None,
            model_usage=model_usage,
        )
        await record_ticket_route_quality(
            session,
            agent_id=agent_id,
            bench_version=ticket.bench_version,
            validator_hotkey=payload.validator_hotkey,
            ticket_deadline=ticket.deadline,
            tool_accuracy=report.tool_mean,
            composite=report.composite,
            now=datetime.now(UTC),
        )
        # Append the immutable, hash-chained audit entry for this score in the
        # same transaction (durable iff the score is). Records the full signed
        # tuple + signature so the entry is independently verifiable off the
        # public audit feed, never any per-case answer-key content.
        await append_audit_entry(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            event=EVENT_SCORE,
            payload={
                "run_id": report.run_id,
                "seed": report.seed,
                "composite": report.composite,
                "tool_mean": report.tool_mean,
                "memory_mean": report.memory_mean,
                "median_ms": report.median_ms,
                "n": report.n,
                "bench_version": ticket.bench_version,
                "ticket_deadline": _lease_token(payload.ticket_deadline),
                "transcript_sha256": _reported_transcript_sha256(report),
                "base_evidence_sha256": report.base_evidence_sha256,
                "signature": payload.signature,
                "generated_at": report.generated_at.isoformat(),
            },
            recorded_at=audit_now,
        )
        if replacement_event is not None:
            replacement_scores = await list_scores_for_agent(
                session, agent_id=agent_id, bench_version=ticket.bench_version
            )
            replacement_dataset = await session.get(
                BenchmarkDataset, (agent_id, ticket.bench_version)
            )
            replacement_median = statistics.median(
                score.composite for score in replacement_scores
            )
            await _evaluate_and_record_deferred_review(
                session,
                agent=agent,
                bench_version=ticket.bench_version,
                score_count=len(replacement_scores),
                settings=queue_policy.deferred_source_review,
                now=audit_now,
            )
            await append_audit_entry(
                session,
                agent_id=agent_id,
                validator_hotkey=None,
                event=EVENT_FINALIZED,
                payload={
                    "miner_hotkey": agent.miner_hotkey,
                    "median_composite": replacement_median,
                    "quorum": SCORING_QUORUM,
                    "score_count": len(replacement_scores),
                    "validator_hotkeys": sorted(
                        score.validator_hotkey for score in replacement_scores
                    ),
                    "dataset_seed": (
                        replacement_dataset.seed
                        if replacement_dataset is not None
                        else agent.dataset_seed
                    ),
                    "dataset_sha256": (
                        replacement_dataset.sha256
                        if replacement_dataset is not None
                        else agent.dataset_sha256
                    ),
                    "dataset_seed_block": (
                        replacement_dataset.seed_block
                        if replacement_dataset is not None
                        else agent.dataset_seed_block
                    ),
                    "dataset_seed_block_hash": (
                        replacement_dataset.seed_block_hash
                        if replacement_dataset is not None
                        else agent.dataset_seed_block_hash
                    ),
                    "status": agent.status.value,
                    "replacement_request_id": replacement_event.payload["request_id"],
                    "replaced_run_id": replacement_event.payload["run_id"],
                },
                recorded_at=audit_now,
            )
            await _publish_finalized_run(
                storage,
                agent=agent,
                scores=replacement_scores,
                median=replacement_median,
                dataset=replacement_dataset,
            )
        # Persist the crate's structural (AST) fingerprint from the report, so it
        # is available for the gate here and for future cross-miner comparison.
        # Advisory + unsigned: only overwrite when the report actually carries one,
        # so a re-score by a scorer that omits it never wipes a stored sketch.
        if report.structural_fingerprint is not None:
            agent.structural_fingerprint = report.structural_fingerprint.model_dump()
        # Finalize at quorum (k=3): an agent stays provisional (``evaluating``)
        # until :data:`SCORING_QUORUM` validators have scored it; only the
        # quorum-th score moves it ``evaluating -> scored``, unless the anti-copy
        # gate holds a suspected copy in ``ath_pending_review``. Both the gate
        # and the transition run on the **median** composite, so no single
        # validator's score decides an agent's fate. The gate runs only on this
        # one transition; a re-score of an already-scored (or held) agent leaves
        # its status put so re-reporting never thrashes the ledger. The agent is
        # still ``evaluating`` here, so it is not yet in the eligible ledger (no
        # self-match). A below-quorum score just records the row and waits.
        if agent.status == AgentStatus.EVALUATING:
            agent_scores = await list_scores_for_agent(
                session, agent_id=agent_id, bench_version=ticket.bench_version
            )
            if len(agent_scores) >= SCORING_QUORUM:
                median_composite = statistics.median(s.composite for s in agent_scores)
                finalized_dataset = await session.get(
                    BenchmarkDataset, (agent_id, ticket.bench_version)
                )
                # Historical backfill finalizes against its own frozen era.
                # Never compare a v6 artifact to the active v7 anti-copy pool.
                eligible = await list_eligible_ledger(
                    session, bench_version=ticket.bench_version
                )
                miner_coldkey = await get_miner_coldkey_for_agent(
                    session, agent_id=agent_id
                )
                # The ledger above is one row per attested payment owner, which
                # is the right pool to *detect* a copy against and the wrong one
                # to *attribute* it with: owner reduction discards an owner's own
                # earlier generations and the originator's early submissions.
                # This is the unreduced history, used only to name the earliest
                # artifact carrying a match and to admit this owner's own prior
                # work as an alibi. No copy rule triggers on it.
                eligible_history = await list_anti_copy_history(
                    session,
                    bench_version=ticket.bench_version,
                    before=agent.created_at,
                )
                # Hotkeys cryptographically proven to be this same operator. A
                # rotated miner is not a copier of their own earlier work; the
                # coldkey exemption above cannot see that, because rotating is
                # exactly what broke coldkey equality. Copy screening only --
                # this never reaches emission_owner_key.
                linked_hotkeys = frozenset(
                    link.hotkey
                    for link in await list_linked_hotkeys(
                        session,
                        hotkey=agent.miner_hotkey,
                        netuid=expected_netuid(),
                    )
                )
                # Which earlier artifacts the subnet had itself published by the
                # time this one was uploaded. Read against the release policy as
                # it stood *then*, not as it stands now: judging a past upload
                # by today's embargo would retroactively change what the miner
                # could have downloaded. Under `disclosure = never` this is
                # empty and every copy rule fires exactly as before.
                submitted_at_utc = (
                    agent.created_at.replace(tzinfo=UTC)
                    if agent.created_at.tzinfo is None
                    else agent.created_at
                )
                release_policy = await artifact_release_policy_as_of(
                    session, at=submitted_at_utc
                )
                published_before = await list_public_source_releases(
                    session,
                    agent_ids=[e.agent_id for e in eligible],
                    quorum=SCORING_QUORUM,
                    policy=release_policy,
                )
                decision = evaluate_duplicate_signals(
                    agent_id=agent_id,
                    miner_hotkey=agent.miner_hotkey,
                    miner_coldkey=miner_coldkey,
                    linked_owner_hotkeys=linked_hotkeys,
                    public_source_releases=[
                        PublicSourceRelease(
                            agent_id=released_id, available_at=available_at
                        )
                        for released_id, available_at in published_before.items()
                    ],
                    submitted_at=agent.created_at,
                    sha256=agent.sha256,
                    composite=median_composite,
                    size_bytes=agent.size_bytes,
                    normalized_source_hash=agent.normalized_source_hash,
                    content_fingerprint=agent.content_fingerprint,
                    structural_fingerprint=agent.structural_fingerprint,
                    prompt_fingerprint=agent.prompt_fingerprint,
                    eligible=eligible,
                    eligible_history=eligible_history,
                )
                if not decision.held:
                    # An artifact an operator already rejected, re-uploaded as a
                    # fresh agent row. This is deliberately a *second* gate
                    # rather than another rule inside the first: every copy rule
                    # skips the candidate's own owner, and here same-owner
                    # resubmission is the case we are looking for.
                    resubmission = evaluate_rejected_resubmission(
                        agent_id=agent_id,
                        submitted_at=agent.created_at,
                        sha256=agent.sha256,
                        normalized_source_hash=agent.normalized_source_hash,
                        content_fingerprint=agent.content_fingerprint,
                        rejected=await list_rejected_artifacts(
                            session, before=submitted_at_utc
                        ),
                    )
                    # Only *replace* the copy verdict when this gate actually
                    # holds. A not-held decision still carries the copy gate's
                    # `no_copy_opportunity` withdrawal, which the `else` branch
                    # below writes to the immutable audit chain; overwriting it
                    # with a bare not-held would silently drop that record.
                    if resubmission.held:
                        decision = resubmission
                if decision.held:
                    reference_provenance = reference_corpus_provenance()
                    agent.status = AgentStatus.ATH_PENDING_REVIEW
                    agent.duplicate_of = decision.duplicate_of
                    agent.review_reason = decision.reason
                    session.add(
                        AthReview(
                            review_id=uuid4(),
                            agent_id=agent.agent_id,
                            status="pending",
                            opened_at=audit_now,
                            original_duplicate_of=decision.duplicate_of,
                            original_reason=decision.reason,
                            original_policy_version=agent.screening_policy_version,
                            original_evidence={
                                "content_fingerprint_version": (
                                    agent.content_fingerprint or {}
                                ).get("v"),
                                "structural_fingerprint_version": (
                                    agent.structural_fingerprint or {}
                                ).get("v"),
                                "prompt_fingerprint_version": (
                                    agent.prompt_fingerprint or {}
                                ).get("v"),
                            },
                            algorithm_provenance={
                                "snapshot": "score-finalization",
                                "algorithm_version": ANTI_COPY_ALGORITHM_VERSION,
                                "canonical_reference_revision": (
                                    reference_provenance["revision"]
                                ),
                                "reference_corpus_id": reference_provenance[
                                    "corpus_id"
                                ],
                                "reference_exclusion_mode": reference_provenance[
                                    "exclusion_mode"
                                ],
                                "backfilled": False,
                                "opened_at_source": "agent_finalized_audit",
                            },
                        )
                    )
                    logger.warning(
                        "agent %s held for copy review: %s", agent_id, decision.reason
                    )
                else:
                    agent.status = AgentStatus.SCORED
                    if decision.no_copy_opportunity is not None:
                        # A copy signal fired and was withdrawn. Record the match
                        # on the immutable public chain -- an operator asking
                        # "did anything reproduce this artifact" must still find
                        # the answer -- while leaving `duplicate_of` and
                        # `review_reason` unset, because those two columns are
                        # the hold record and the board renders them as an
                        # accusation this miner has not earned.
                        withdrawal = decision.no_copy_opportunity
                        await append_audit_entry(
                            session,
                            agent_id=agent_id,
                            validator_hotkey=None,
                            event=EVENT_COPY_NO_OPPORTUNITY,
                            payload={
                                "kind": withdrawal.kind,
                                "signal": withdrawal.signal,
                                "matched_agent_id": str(withdrawal.matched_agent_id),
                                "source_agent_id": str(withdrawal.source_agent_id),
                                "source_available_at": (
                                    withdrawal.source_available_at.isoformat()
                                    if withdrawal.source_available_at is not None
                                    else None
                                ),
                                "disclosure": release_policy.disclosure.value,
                                "embargo_hours": release_policy.embargo_hours,
                                "algorithm_version": ANTI_COPY_ALGORITHM_VERSION,
                                "detail": withdrawal.detail,
                            },
                            recorded_at=audit_now,
                        )
                        logger.info(
                            "agent %s copy signal withdrawn (%s): %s",
                            agent_id,
                            withdrawal.kind,
                            withdrawal.detail,
                        )
                # Reproduce-under-transform audit (v3 Part A). A share of every
                # run's cases is re-asked under a transform derived from the
                # block-hash-seeded dataset seed, which postdates the commit; the
                # validator reports the median robustness over its confirmation
                # runs. Below the public floor, the agent goes to review instead
                # of scored, so it is excluded from emissions until an operator
                # resolves it -- exactly like the copy-review hold.
                #
                # Quarantine-then-review, never an auto-ban: a low value is the
                # surface-brittleness or memorization signature, and it is NOT
                # evidence about a harness that genuinely recomputes its answers
                # (that one scores the same under the transform). It reuses
                # ATH_PENDING_REVIEW with a distinct review_reason rather than
                # adding a sibling status, which would force a
                # ditto-screening-protocol pin bump across every consumer for a
                # distinction the reason string already carries.
                audit_pvalue, audit_pairs, audit_failed = _transform_audit_verdict(
                    agent_scores
                )
                if audit_failed and not TRANSFORM_AUDIT_ENFORCE:
                    # Observational mode: record what the verdict WOULD have been
                    # (the EVENT_AUDIT entry below carries `failed`) without
                    # touching the agent's status. This is what accumulates the
                    # real-world distribution a future threshold can be set from.
                    logger.info(
                        "agent %s: transform-audit brittleness signature "
                        "(%d base-only vs %d transform-only, p=%.4f <= %.3f) "
                        "— NOT enforced pending champion-population validation",
                        agent_id,
                        audit_pairs["base_only"],
                        audit_pairs["transform_only"],
                        audit_pvalue if audit_pvalue is not None else 1.0,
                        AUDIT_ALPHA,
                    )
                if (
                    audit_failed
                    and TRANSFORM_AUDIT_ENFORCE
                    and agent.status == AgentStatus.SCORED
                ):
                    agent.status = AgentStatus.ATH_PENDING_REVIEW
                    agent.review_reason = TRANSFORM_AUDIT_REVIEW_REASON
                    session.add(
                        AthReview(
                            review_id=uuid4(),
                            agent_id=agent.agent_id,
                            status="pending",
                            opened_at=audit_now,
                            original_reason=TRANSFORM_AUDIT_REVIEW_REASON,
                            original_policy_version=agent.screening_policy_version,
                            original_evidence={
                                "audit_pairs": audit_pairs,
                                "transform_audit_pvalue": audit_pvalue,
                                "audit_alpha": AUDIT_ALPHA,
                            },
                            algorithm_provenance={
                                "snapshot": "score-finalization",
                                "opened_at_source": "transform_audit",
                            },
                        )
                    )
                    logger.warning(
                        "agent %s held for transform-audit review: %d base-only "
                        "vs %d transform-only discordant pairs, p=%.4f <= %.3f",
                        agent_id,
                        audit_pairs["base_only"],
                        audit_pairs["transform_only"],
                        audit_pvalue if audit_pvalue is not None else 1.0,
                        AUDIT_ALPHA,
                    )
                if sum(audit_pairs.values()) > 0:
                    # Recorded whether or not it held, so the public feed shows
                    # the audit ran and what it found -- not only its failures.
                    # PUBLIC INPUTS ONLY: never a transformed expected answer or
                    # any other answer-key material, the same redaction rule the
                    # score entry follows. Everything here is either already
                    # published or re-derivable from the published seed, so a
                    # third party can recompute this verdict independently.
                    await append_audit_entry(
                        session,
                        agent_id=agent_id,
                        validator_hotkey=None,
                        event=EVENT_AUDIT,
                        payload={
                            "miner_hotkey": agent.miner_hotkey,
                            "audit_pairs": audit_pairs,
                            "transform_audit_pvalue": audit_pvalue,
                            "audit_alpha": AUDIT_ALPHA,
                            "audit_bps": AUDIT_BPS,
                            "failed": audit_failed,
                            # Whether the verdict was allowed to affect status.
                            # Published so the feed is unambiguous about which
                            # entries were observational.
                            "enforced": TRANSFORM_AUDIT_ENFORCE,
                            "dataset_seed": (
                                finalized_dataset.seed
                                if finalized_dataset is not None
                                else agent.dataset_seed
                            ),
                            "dataset_sha256": (
                                finalized_dataset.sha256
                                if finalized_dataset is not None
                                else agent.dataset_sha256
                            ),
                            "dataset_seed_block": (
                                finalized_dataset.seed_block
                                if finalized_dataset is not None
                                else agent.dataset_seed_block
                            ),
                            "dataset_seed_block_hash": (
                                finalized_dataset.seed_block_hash
                                if finalized_dataset is not None
                                else agent.dataset_seed_block_hash
                            ),
                            "score_count": len(agent_scores),
                        },
                        recorded_at=audit_now,
                    )
                # Out-of-band composite escalation (issue #476). After the
                # anti-copy and transform-audit gates have had their say, a
                # composite that is a robust upward outlier of its comparable
                # same-benchmark cohort is routed to ATH review instead of
                # ranked -- the safety net for a gamed fresh contract whose
                # spike outruns the other gates. Bench-version scoped (v12+),
                # so v8-v11 are untouched, and a no-op unless an operator turns
                # it on. The cohort is the eligible ledger read above, which is
                # one row per owner and excludes this still-evaluating
                # candidate, held agents, and banned agents.
                await _evaluate_and_record_outlier_escalation(
                    session,
                    agent=agent,
                    bench_version=ticket.bench_version,
                    composite=median_composite,
                    cohort=[row.composite for row in eligible],
                    settings=OUTLIER_ESCALATION_SETTINGS,
                    now=audit_now,
                )
                deferred_settings = queue_policy.deferred_source_review
                await _evaluate_and_record_deferred_review(
                    session,
                    agent=agent,
                    bench_version=ticket.bench_version,
                    score_count=len(agent_scores),
                    settings=deferred_settings,
                    now=audit_now,
                )
                # Append the finalize audit entry: quorum reached, the median the
                # platform finalized on, and which validators scored it. The
                # moderation detail (why held / duplicate_of) is deliberately kept
                # out of the public chain — only the neutral outcome status.
                await append_audit_entry(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=None,
                    event=EVENT_FINALIZED,
                    payload={
                        "miner_hotkey": agent.miner_hotkey,
                        "median_composite": median_composite,
                        "bench_version": ticket.bench_version,
                        "quorum": SCORING_QUORUM,
                        "score_count": len(agent_scores),
                        "validator_hotkeys": sorted(
                            s.validator_hotkey for s in agent_scores
                        ),
                        "dataset_seed": (
                            finalized_dataset.seed
                            if finalized_dataset is not None
                            else agent.dataset_seed
                        ),
                        "dataset_sha256": (
                            finalized_dataset.sha256
                            if finalized_dataset is not None
                            else agent.dataset_sha256
                        ),
                        "dataset_seed_block": (
                            finalized_dataset.seed_block
                            if finalized_dataset is not None
                            else agent.dataset_seed_block
                        ),
                        "dataset_seed_block_hash": (
                            finalized_dataset.seed_block_hash
                            if finalized_dataset is not None
                            else agent.dataset_seed_block_hash
                        ),
                        "status": agent.status.value,
                    },
                    recorded_at=audit_now,
                )
                # Transparency mirror: publish the finalized run record to the
                # public bucket so third parties can verify signatures and
                # re-grade offline without touching the API. Additive and
                # fail-open: the canonical record is Postgres; a publish
                # failure logs and never fails the score write. Idempotent by
                # key, so a retried request republishes identical content.
                await _publish_finalized_run(
                    storage,
                    agent=agent,
                    scores=agent_scores,
                    median=median_composite,
                    dataset=finalized_dataset,
                )
        elif existing_score is None and agent.status in {
            AgentStatus.SCORED,
            AgentStatus.LIVE,
        }:
            # Rollout members were already finalized in the source era, so the
            # global agent status must not transition again when their desired-
            # version quorum arrives. Still emit the version-scoped final audit
            # and mirror exactly when the third distinct score establishes that
            # era's canonical median.
            migrated_scores = await list_scores_for_agent(
                session, agent_id=agent_id, bench_version=ticket.bench_version
            )
            if len(migrated_scores) >= SCORING_QUORUM:
                migrated_dataset = await session.get(
                    BenchmarkDataset, (agent_id, ticket.bench_version)
                )
                migrated_median = statistics.median(
                    score.composite for score in migrated_scores
                )
                await append_audit_entry(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=None,
                    event=EVENT_FINALIZED,
                    payload={
                        "miner_hotkey": agent.miner_hotkey,
                        "median_composite": migrated_median,
                        "bench_version": ticket.bench_version,
                        "quorum": SCORING_QUORUM,
                        "score_count": len(migrated_scores),
                        "validator_hotkeys": sorted(
                            score.validator_hotkey for score in migrated_scores
                        ),
                        "dataset_seed": (
                            migrated_dataset.seed
                            if migrated_dataset is not None
                            else agent.dataset_seed
                        ),
                        "dataset_sha256": (
                            migrated_dataset.sha256
                            if migrated_dataset is not None
                            else agent.dataset_sha256
                        ),
                        "dataset_seed_block": (
                            migrated_dataset.seed_block
                            if migrated_dataset is not None
                            else agent.dataset_seed_block
                        ),
                        "dataset_seed_block_hash": (
                            migrated_dataset.seed_block_hash
                            if migrated_dataset is not None
                            else agent.dataset_seed_block_hash
                        ),
                        "status": agent.status.value,
                    },
                    recorded_at=audit_now,
                )
                await _publish_finalized_run(
                    storage,
                    agent=agent,
                    scores=migrated_scores,
                    median=migrated_median,
                    dataset=migrated_dataset,
                )
        # Consume the ticket (one ticket, one score); the slot stays occupied.
        await mark_ticket_scored(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            bench_version=ticket.bench_version,
        )
        await revoke_ticket_inference(session, ticket=ticket, now=audit_now)
        # Never activate the operator re-test queue from the score transaction.
        # This transaction already owns the agent and completed ticket rows;
        # activation deliberately locks the validator and all of its issued
        # tickets. Validators with historical re-test lifecycle entries would
        # therefore recreate the agent/ticket lock-order inversion fixed in
        # #558 and lose a finished benchmark to Postgres deadlock recovery.
        # The validator's immediate next job poll activates the queue under its
        # normal issuance lock, and admin queue actions activate it directly, so
        # removing this eager handoff delays nothing and leaves one lock owner.
        result_status = agent.status

    # LongMem's expensive dimensions live on a separate bounded ledger. Once the
    # canonical score transaction commits, persist its physical lower-median
    # signed base proof and converge the owner/top-N cohort in a fresh
    # transaction. This projection is deliberately fail-open: a profile,
    # migration, or auxiliary-ledger fault must never roll back an accepted
    # ordinary score or consumed ticket. Pre-claim/settings reconciliation
    # converges any missed projection before expensive work can start.
    #
    # The cohort is the LIVE benchmark, never the version this report happens to
    # carry. A straggler score for a superseded epoch must not manufacture new
    # confirmation work no one will ever rank — that is exactly how the lane
    # stayed pinned to a retired v9 cohort while the network ran on v11.
    if supports_confirmation(report_version):
        try:
            async with session.begin():
                confirmation_version = await active_bench_version(session)
                if report_version == confirmation_version:
                    finalized_agent = await get_agent_by_id(
                        session, agent_id=agent_id, for_update=True
                    )
                    finalized_base_scores = await list_scores_for_agent(
                        session, agent_id=agent_id, bench_version=confirmation_version
                    )
                    if (
                        finalized_agent is not None
                        and len(finalized_base_scores) >= SCORING_QUORUM
                    ):
                        await reconcile_confirmation_candidates(
                            session,
                            bench_version=confirmation_version,
                            verification_profiles=getattr(
                                request.app.state,
                                "confirmation_verification_profiles",
                                {},
                            ),
                            finalized_agent=finalized_agent,
                            finalized_scores=finalized_base_scores,
                        )
        except Exception:
            logger.exception(
                "confirmation candidate reconciliation failed for agent %s",
                agent_id,
            )

    # Both a completed v3 quorum and a newly finalized v2 contender can change
    # the hybrid top five. This is a cheap no-op when no rollout is open.
    try:
        await refresh_rolling_qualification(
            session,
            generator=generator,
            now=audit_now,
            inference_config=request.app.state.config.inference_proxy,
        )
    except Exception:
        # The score is already committed and remains canonical. Do not report a
        # false score failure because the independent v3 dataset renderer is
        # temporarily unavailable; the next score/verdict/admin retry converges.
        logger.exception("rolling benchmark qualification refresh failed")

    # Record when the CURRENT KOTH champion first took the throne, so the
    # king-only public source-release embargo can reveal its source one window
    # later. This reads whoever is champion NOW (any committed score, including
    # a confirmation-driven dethrone, can have changed it), not the submitter.
    # Post-commit and best-effort: the score is already canonical, so a
    # recording hiccup must never surface as a score failure, and the write-once
    # timestamp is never moved by a later re-coronation.
    try:
        crown_efficiency_config = await request.app.state.efficiency_settings.resolve(
            getattr(request.app.state, "session_maker", None)
        )
        if crown_efficiency_config.enabled:
            await ensure_current_efficiency_state(
                request.app.state,
                session,
                crown_efficiency_config,
                now=audit_now,
            )
        async with session.begin():
            champion_members = await _current_emission_set(
                session,
                canonical_version=await active_bench_version(session),
                efficiency_config=crown_efficiency_config,
                now=audit_now,
            )
            if champion_members:
                await record_first_crowned(
                    session,
                    agent_id=champion_members[0].agent_id,
                    now=audit_now,
                )
    except Exception:
        logger.exception("king-reign recording failed")

    # Arm a king's public source-release window only once the chain agrees:
    # confirm (post commit-reveal) that validators' REVEALED weights are set on
    # the miner. Best-effort and post-commit: never fail an already-canonical
    # score because a chain read hiccups.
    try:
        await _confirm_king_onchain_weights(
            request.app.state, chain, session, now=audit_now
        )
    except (ChainError, TimeoutError):
        logger.warning("king weight-confirmation chain read failed", exc_info=True)
    except Exception:
        logger.exception("king weight-confirmation failed")

    logger.info(
        "score recorded agent_id=%s validator=%s run_id=%s composite=%.3f status=%s",
        agent_id,
        payload.validator_hotkey,
        report.run_id,
        report.composite,
        result_status,
    )
    return SubmitScoreResponse(agent_id=agent_id, status=result_status, accepted=True)


async def _publish_finalized_run(
    storage: S3StorageClient,
    *,
    agent: Agent,
    scores: Sequence[Score],
    median: float,
    dataset: BenchmarkDataset | None = None,
) -> None:
    """Mirror a finalized run to version-addressed and current public keys.

    The record carries everything an offline verifier needs: the dataset pin
    (seed, sha256, seed block), the k=3 signed scores with their full details
    (per-case breakdown included), and the median the platform finalized on.
    Current signatures cover
    ``{hotkey}:{agent_id}:{ticket_deadline}:{run_id}:{composite!r}:{seed}``;
    legacy scores have no ``ticket_deadline`` detail and retain the previous
    payload format. The record therefore carries the lease identity needed to
    verify either generation against the validator's on-chain hotkey.
    No-op when ``STORAGE_PUBLIC_BUCKET`` is unset; failures log only.
    """
    if storage.public_bucket is None:
        return
    bench_version = scores[0].bench_version if scores else None
    record = {
        "agent_id": str(agent.agent_id),
        "miner_hotkey": agent.miner_hotkey,
        "status": agent.status.value,
        "median_composite": median,
        "bench_version": bench_version,
        "dataset_seed": dataset.seed if dataset is not None else agent.dataset_seed,
        "dataset_sha256": (
            dataset.sha256 if dataset is not None else agent.dataset_sha256
        ),
        "dataset_run_size": (
            dataset.run_size if dataset is not None else agent.dataset_run_size
        ),
        "dataset_seed_block": (
            dataset.seed_block if dataset is not None else agent.dataset_seed_block
        ),
        "dataset_seed_block_hash": (
            dataset.seed_block_hash
            if dataset is not None
            else agent.dataset_seed_block_hash
        ),
        "scores": [
            {
                "validator_hotkey": sc.validator_hotkey,
                "run_id": sc.run_id,
                "ticket_deadline": (
                    sc.details.get("ticket_deadline")
                    if isinstance(sc.details, dict)
                    else None
                ),
                "seed": sc.seed,
                "composite": sc.composite,
                "tool_mean": sc.tool_mean,
                "memory_mean": sc.memory_mean,
                "median_ms": sc.median_ms,
                "n": sc.n,
                "generated_at": sc.generated_at.isoformat()
                if sc.generated_at
                else None,
                "signature": sc.signature,
                # Where the validator's transcript artifact lives (finding 3):
                # the digest is inside the signed payload; the key is derived
                # from it, so the record always names immutable bytes. Null for
                # scores whose validator published no transcript.
                "transcript_sha256": digest,
                "transcript_key": transcript_object_key(digest) if digest else None,
                "base_evidence_sha256": (
                    sc.details.get("base_evidence_sha256")
                    if isinstance(sc.details, dict)
                    else None
                ),
                "details": sc.details,
            }
            for sc, digest in (
                (sc, _score_transcript_sha256(sc))
                for sc in sorted(scores, key=lambda sc: sc.validator_hotkey)
            )
        ],
    }
    body = json.dumps(record, sort_keys=True, default=str).encode()
    keys = (
        [f"scored/{agent.agent_id}/v{bench_version}.json"]
        if bench_version is not None
        else []
    )
    keys.append(f"scored/{agent.agent_id}.json")
    for key in keys:
        try:
            await storage.put_object(
                key=key,
                body=body,
                content_type="application/json",
                bucket=storage.public_bucket,
            )
        except Exception:  # noqa: BLE001 - additive mirror, never fail the write
            logger.exception(
                "public mirror publish failed for agent %s key %s",
                agent.agent_id,
                key,
            )


# Transcript artifacts are content-addressed in the public bucket so a record
# referencing a digest always names immutable bytes.
_TRANSCRIPT_KEY_TEMPLATE = "transcripts/{sha256}.json"

# A transcript carries every graded final_text for a full run; cap well above
# any legitimate size while bounding a hostile body.
_TRANSCRIPT_MAX_BYTES = 32 << 20

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def transcript_object_key(sha256_hex: str) -> str:
    """Public-bucket key for a transcript digest."""
    return _TRANSCRIPT_KEY_TEMPLATE.format(sha256=sha256_hex)


def _score_transcript_sha256(score: Score) -> str | None:
    """The well-formed transcript digest a stored score declares, or ``None``."""
    details = score.details if isinstance(score.details, dict) else {}
    value = details.get("transcript_sha256")
    if isinstance(value, str) and _SHA256_HEX.fullmatch(value):
        return value
    return None


@router.put(
    "/agent/{agent_id}/transcript/{run_id}",
    response_model=SubmitTranscriptResponse,
)
async def submit_transcript(
    agent_id: UUID,
    run_id: str,
    request: Request,
    response: Response,
    session: SessionDep,
    validator: ValidatorDep,
    storage: StorageDep,
) -> SubmitTranscriptResponse:
    """Publish the transcript artifact behind a signed score (finding 3).

    The body is the scoring engine's canonical transcript for ``run_id`` — the
    graded per-case inputs whose digest the validator declared under
    ``details["transcript_sha256"]`` and bound into its score signature. The
    platform accepts the bytes only when their SHA-256 equals that declared
    digest, then stores them content-addressed in authoritative storage and
    mirrors them publicly when configured. Because the binding is *content*
    equality against an already-signed digest, a
    caller spoofing another validator's hotkey can only ever upload the exact
    bytes that validator attested — so the header + permit check is sufficient
    auth here. Idempotent: re-uploading an existing digest is a no-op.
    """
    response.headers["Cache-Control"] = "no-store"
    body = await request.body()
    if len(body) > _TRANSCRIPT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="transcript exceeds size cap")
    if not body:
        raise HTTPException(status_code=400, detail="empty transcript body")
    digest = hashlib.sha256(body).hexdigest()

    score = await get_score_for_validator(
        session, agent_id=agent_id, validator_hotkey=validator
    )
    if score is None or score.run_id != run_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "no recorded score by this validator for this agent and run; "
                "submit the score (with details.transcript_sha256) first"
            ),
        )
    declared = (
        score.details.get("transcript_sha256")
        if isinstance(score.details, dict)
        else None
    )
    if not isinstance(declared, str) or not _SHA256_HEX.fullmatch(declared):
        raise HTTPException(
            status_code=409,
            detail="the recorded score declares no transcript_sha256",
        )
    if digest != declared:
        raise HTTPException(
            status_code=409,
            detail=(
                f"transcript bytes hash to {digest} but the signed score "
                f"declared {declared}"
            ),
        )

    key = transcript_object_key(digest)
    # The primary bucket is authoritative so transcript-backed dashboard
    # telemetry works even when no anonymous transparency bucket is configured.
    if not await storage.object_exists(key=key):
        await storage.put_object(
            key=key,
            body=body,
            content_type="application/json",
        )
        logger.info(
            "transcript published agent_id=%s run_id=%s sha256=%s bytes=%d",
            agent_id,
            run_id,
            digest,
            len(body),
        )
    # Preserve the optional anonymous mirror for offline auditors. A mirror
    # outage must not discard the authoritative transcript after score
    # acceptance.
    if storage.public_bucket is not None:
        try:
            if not await storage.object_exists(key=key, bucket=storage.public_bucket):
                await storage.put_object(
                    key=key,
                    body=body,
                    content_type="application/json",
                    bucket=storage.public_bucket,
                )
        except Exception:  # noqa: BLE001 - additive mirror, primary already stored
            logger.exception("public transcript mirror failed for %s", digest)
    return SubmitTranscriptResponse(
        agent_id=agent_id, run_id=run_id, transcript_sha256=digest, stored=True
    )

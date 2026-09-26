"""The terminal-review emission gate: one classifier, one resolver, one record.

``GET /scoring/scores`` decides who the validator fold may pay. Today its only
review filter is ``agents.status == 'scored'``, which drops an agent parked in
``ath_pending_review`` but *not* the state
``apps/platform/docs/ath-review-queue.md`` calls a **stranded hold**: an
``ath_reviews`` row still ``pending`` while ``agents.status`` has moved back to
``scored``. That row folds into KOTH and earns emissions with its review open,
and a later rejection makes the subnet's own payments the dispute (#2041).

This module closes that in one place, under operator-owned revisioned policy:

* :func:`classify` is a **pure function** from one artifact's review facts plus
  the posture to one :class:`AgentEmissionEligibility`. No I/O, no clock beyond
  the window boundary it is handed, so every consumer -- the validator ledger,
  the epoch pin, the public board, the submission page, Backroom -- reaches the
  same verdict from the same inputs. That is #2041's "validator folds and public
  projections consume the same eligibility record".
* :func:`evaluate_ledger` applies it across a ledger and reports the withheld
  set *without* deciding what to do about it. The caller applies it only under
  ``enforce``.
* :class:`EmissionEligibilityResolver` serves the posture with the same short
  TTL and the same fallback direction as ``BurnSettingsResolver``: an
  unreadable or malformed revision degrades to :data:`DEFAULT_SETTINGS`
  (enforcement off), because failing this read closed would stop emissions
  subnet-wide over a parsing error, and the conservative direction here is to
  keep paying miners.

What this module never does
---------------------------

* **Hide a score.** Nothing here touches the board, the score row, the review
  history, or the artifact. A withheld artifact keeps its published composite,
  its rank among scored rows, and its full review trail, before and after a
  rejection.
* **Pay backwards.** A terminal clear is honoured from the next window
  (:func:`~ditto.api_models.emission_eligibility.window_start`). There is no
  back-pay path and no clawback path, by construction: the only thing the gate
  can do is include or exclude a row from the *next* fold.
* **Blame a miner for Ditto's fault.** An infrastructure failure is published as
  Ditto's, retried automatically by ``screening_infra_retry``, and never written
  as a violation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError

from ditto.api_models.emission_eligibility import (
    DEFAULT_SETTINGS,
    STATE_REASONS,
    AgentEmissionEligibility,
    EligibilityState,
    EmissionEligibilitySettings,
    eligibility_checksum,
    next_window_start,
    window_start,
)
from ditto.api_server.deferred_source_review import INCONCLUSIVE_REASON_CODE
from ditto.api_server.outlier_escalation import OUTLIER_REVIEW_KIND
from ditto.db.queries.emission_eligibility import AgentReviewPosture
from ditto.db.queries.screening import (
    EXHAUSTED_REASON_CODE,
    PROVIDER_BACKOFF_REASON_CODES,
)
from ditto.db.queries.screening_infra_retry import INFRA_AUTO_RETRY_REASON_CODES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import (
        EmissionEligibilitySettingsRevision as EligibilityRevisionRow,
    )
    from ditto.db.queries.scores import LedgerRow

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_TTL_SECONDS = 5.0
"""Same TTL as the burn resolver, for the same reason: a Backroom flip must land
on the next ledger read without a restart, and the ledger read is hot."""

INCONCLUSIVE_REASON_CODES: frozenset[str] = frozenset(
    {INCONCLUSIVE_REASON_CODE, EXHAUSTED_REASON_CODE}
)
"""Review finished without a finding, one way or another.

``source-review-inconclusive`` is the bounded review running out of budget
(#2077 established that this is *not* an accusation, and the published reason
says so). ``repeatedly-inconclusive`` is the platform's own exhaustion hold
after ``MAX_SCREENING_EXPIRIES``. Both mean mandatory verification is
incomplete, which #2051 says must not earn.
"""

INFRASTRUCTURE_REASON_CODES: frozenset[str] = frozenset(
    (*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES)
)
"""Ditto's own build/provider failures. Imported rather than copied so adding a
code in one place cannot leave this gate reading the old set."""

ESCALATED_REVIEW_KINDS: frozenset[str] = frozenset({OUTLIER_REVIEW_KIND})
"""Hold kinds that exist *because* something was escalated to an operator."""

COURT_ESCALATION_VERDICT = "escalate"
"""The copy-hold court's own refusal to decide mechanically."""


def settings_from_row(
    row: EligibilityRevisionRow | None,
) -> EmissionEligibilitySettings:
    """The stored posture, or the enforcement-off default.

    An unparseable revision falls back rather than propagating: this is read on
    the path every validator hits before every weight submission, and failing it
    to protect a malformed row would stop weight submission subnet-wide. The
    default is also the direction that keeps paying miners.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return EmissionEligibilitySettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "emission eligibility revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


@dataclass(frozen=True)
class ResolvedEligibilityPolicy:
    """The posture plus the revision identity every record is bound to."""

    settings: EmissionEligibilitySettings = DEFAULT_SETTINGS
    revision: int = 0
    checksum: str = ""
    source: str = "default"

    @property
    def enforcing(self) -> bool:
        return self.settings.enforcement == "enforce"

    @property
    def evaluating(self) -> bool:
        """Whether the gate runs at all. ``off`` skips every read below."""
        return self.settings.enforcement in ("shadow", "enforce")


DEFAULT_POLICY = ResolvedEligibilityPolicy()


def _state_for(
    posture: AgentReviewPosture,
    settings: EmissionEligibilitySettings,
    *,
    window: datetime,
) -> EligibilityState:
    """Which of #2041's classes this exact artifact is in, most specific first.

    Order matters and is the issue's own order: a pending hold that the court
    escalated is reported as ``review_escalated``, not as the generic
    ``unresolved_review``, because "escalated" is what an operator has to act on
    and what the miner is owed as an explanation.
    """
    if posture.review_status == "resolved" and posture.review_resolution == "reject":
        # A stranded reject: resolved against the artifact while agents.status
        # drifted back to scored. Never eligible, and never re-payable.
        return "review_rejected"

    held = posture.review_status == "pending"
    reason_codes = {
        posture.active_quarantine_reason_code,
        posture.latest_attempt_reason_code,
    }
    reason_codes.discard(None)

    if settings.exclude_escalated and held:
        if posture.court_verdict == COURT_ESCALATION_VERDICT:
            return "review_escalated"
        if posture.review_kind in ESCALATED_REVIEW_KINDS:
            return "review_escalated"
    # An inconclusive outcome counts while the hold is open, and also when the
    # artifact was readmitted to scoring on it with no terminal decision recorded
    # -- the state #2041 calls "inconclusive source review".
    if (
        settings.exclude_inconclusive
        and (reason_codes & INCONCLUSIVE_REASON_CODES)
        and (held or posture.review_status is None)
    ):
        return "review_inconclusive"
    if (
        settings.exclude_infrastructure_failed
        and (reason_codes & INFRASTRUCTURE_REASON_CODES)
        and (posture.latest_attempt_status in ("failed", "expired") or held)
    ):
        return "review_infrastructure_failed"
    if settings.require_terminal_review and held:
        return "unresolved_review"
    if settings.require_completed_review and posture.passed_attempt_count == 0:
        return "review_missing"
    if (
        posture.review_status == "resolved"
        and posture.review_resolution == "clear"
        and posture.review_resolved_at is not None
        and posture.review_resolved_at.astimezone(window.tzinfo) >= window
    ):
        # The documented next-window rule. A clear landing inside the window a
        # validator is already folding would move the pool under it mid-epoch,
        # and honouring it earlier than the boundary would be a retroactive
        # grant. Both are what #2041 forbids.
        return "awaiting_next_window"
    return "eligible"


def classify(
    *,
    agent_id: UUID,
    artifact_sha256: str,
    bench_version: int | None,
    posture: AgentReviewPosture,
    policy: ResolvedEligibilityPolicy,
    now: datetime,
) -> AgentEmissionEligibility:
    """The shared eligibility record for one exact artifact. Pure."""
    settings = policy.settings
    window = window_start(now, window_seconds=settings.activation_window_seconds)
    state: EligibilityState = (
        "eligible"
        if not policy.evaluating
        else _state_for(posture, settings, window=window)
    )
    posture_satisfied = state == "eligible"
    return AgentEmissionEligibility(
        agent_id=agent_id,
        artifact_sha256=artifact_sha256,
        bench_version=bench_version,
        policy_revision=policy.revision,
        policy_checksum=policy.checksum or eligibility_checksum(settings),
        enforcement=settings.enforcement,
        state=state,
        reason=STATE_REASONS[state],
        # Only ``enforce`` withholds. Under ``off``/``shadow`` the row is paid
        # exactly as before, which is what makes shipping this a no-op.
        reward_eligible=posture_satisfied or not policy.enforcing,
        posture_satisfied=posture_satisfied,
        window_start=window,
        activates_at=(
            next_window_start(now, window_seconds=settings.activation_window_seconds)
            if state == "awaiting_next_window"
            else None
        ),
        review_status=posture.review_status,
        review_resolution=posture.review_resolution,
        review_kind=posture.review_kind,
        review_resolved_at=posture.review_resolved_at,
        screening_reason_code=(
            posture.active_quarantine_reason_code or posture.latest_attempt_reason_code
        ),
    )


@dataclass(frozen=True)
class LedgerEligibility:
    """One evaluation of a whole ledger under one posture and one window."""

    policy: ResolvedEligibilityPolicy
    window_start: datetime
    records: dict[UUID, AgentEmissionEligibility] = field(default_factory=dict)
    """``agent_id -> AgentEmissionEligibility`` for every row evaluated."""

    @property
    def withheld(self) -> list[AgentEmissionEligibility]:
        """Rows ``enforce`` excludes -- the shadow rehearsal's whole output."""
        return [
            record for record in self.records.values() if not record.posture_satisfied
        ]

    def filter_rows(self, rows: Sequence[LedgerRow]) -> list[LedgerRow]:
        """The pool the fold may pay.

        Under ``off``/``shadow`` this returns ``rows`` unchanged, so the served
        ledger is byte-identical to what it was before the gate existed.

        Under ``enforce`` withheld rows are dropped *before* owner dedupe, so an
        owner whose newest generation is held is represented by its best
        generation with a terminal review instead of losing its position
        entirely. A row with no record (never evaluated) is kept: absence of
        evidence is not a reason to stop paying.
        """
        if not self.policy.enforcing:
            return list(rows)
        return [
            row
            for row in rows
            if self.records.get(row.agent_id) is None
            or self.records[row.agent_id].posture_satisfied
        ]


def evaluate_ledger(
    rows: Sequence[LedgerRow],
    postures: Mapping[UUID, AgentReviewPosture],
    *,
    policy: ResolvedEligibilityPolicy,
    now: datetime,
) -> LedgerEligibility:
    """Classify every ledger row. Pure; the caller decides what to apply."""
    window = window_start(now, window_seconds=policy.settings.activation_window_seconds)
    records = {
        row.agent_id: classify(
            agent_id=row.agent_id,
            artifact_sha256=row.sha256,
            bench_version=row.bench_version,
            posture=postures.get(row.agent_id)
            or AgentReviewPosture(agent_id=row.agent_id),
            policy=policy,
            now=now,
        )
        for row in rows
    }
    return LedgerEligibility(policy=policy, window_start=window, records=records)


def shadow_rows(evaluation: LedgerEligibility) -> list[dict]:
    """The append-only rehearsal rows for one evaluation.

    Written under ``shadow`` (the rehearsal) and under ``enforce`` (the audit
    trail for why a row left the fold). ``off`` evaluates nothing, so there is
    nothing to write.
    """
    if not evaluation.policy.evaluating:
        return []
    return [
        {
            "record_id": uuid4(),
            "agent_id": record.agent_id,
            "artifact_sha256": record.artifact_sha256,
            "bench_version": record.bench_version or 0,
            "state": record.state,
            "reason": record.reason,
            "policy_revision": record.policy_revision,
            "policy_checksum": record.policy_checksum,
            "enforcement": record.enforcement,
            "window_start": record.window_start,
        }
        for record in evaluation.withheld
    ]


@dataclass
class _CacheEntry:
    policy: ResolvedEligibilityPolicy
    loaded_at: float


class EmissionEligibilityResolver:
    """Short-TTL resolver for the operator-owned eligibility posture.

    Deliberately identical in shape to ``BurnSettingsResolver``: one cached
    already-resolved answer, invalidated on write, so every validator polling
    inside the TTL reads the same posture and no validator has to evaluate a
    schedule against its own clock.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> ResolvedEligibilityPolicy:
        if session_maker is None:
            return DEFAULT_POLICY
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.policy
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.policy
            from ditto.db.queries.emission_eligibility import (
                latest_eligibility_settings_revision,
            )

            async with session_maker() as session:
                row = await latest_eligibility_settings_revision(session)
            policy = policy_from_row(row)
            self._cache = _CacheEntry(policy=policy, loaded_at=time.monotonic())
            return policy


def policy_from_row(row: EligibilityRevisionRow | None) -> ResolvedEligibilityPolicy:
    settings = settings_from_row(row)
    if row is None or settings is DEFAULT_SETTINGS:
        # Either no revision at all, or one :func:`settings_from_row` refused to
        # parse. Both serve the documented safe default, and both must SAY
        # ``default`` -- reporting ``revision`` while running on the fallback is
        # how an operator ends up believing a posture is live when it is not.
        # The revision number is kept so the unusable row is findable.
        return ResolvedEligibilityPolicy(
            settings=settings,
            revision=row.revision if row is not None else 0,
            checksum=eligibility_checksum(settings),
            source="default",
        )
    return ResolvedEligibilityPolicy(
        settings=settings,
        revision=row.revision,
        checksum=row.checksum,
        source="revision",
    )

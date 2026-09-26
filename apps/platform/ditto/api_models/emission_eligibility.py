"""Operator-owned policy binding reward eligibility to a terminal review.

A submission can lead the public board while the *exact artifact* behind that
score still has an unresolved, inconclusive, infrastructure-failed, or escalated
source review (ditto-subnet #2041). ``list_eligible_ledger`` already drops an
agent parked in ``ath_pending_review``, but the production state this module
exists for is the one ``apps/platform/docs/ath-review-queue.md`` calls a
*stranded hold*: ``ath_reviews.status = 'pending'`` while ``agents.status`` has
moved back to ``scored``. Such a row is in the ledger, folds into KOTH, and earns
emissions with its review still open. If it is later rejected, the subnet has
paid for an artifact it then disowns -- a preventable fairness dispute.

The gate is shaped exactly like ``burn_share``
(:mod:`ditto.api_models.burn_settings`) and the copy-hold court
(:mod:`ditto.api_models.copy_court_settings`), for the same reasons:

* **Platform-owned and revisioned.** This moves TAO, so "what was the posture
  when that vector was folded, who set it, and why" has to be answerable from
  the database. Every write is an append-only revision with
  ``expected_revision`` optimistic concurrency, a typed confirmation phrase, an
  actor, and a reason.
* **Served already-resolved.** The validator never evaluates this policy. The
  platform decides the eligible pool -- which is authority it already has, since
  serving an empty ledger routes 100% of miner emission to burn today -- and the
  fleet reads one decided answer on the same ledger poll it already makes.
* **Default off.** :data:`DEFAULT_SETTINGS` is ``enforcement="off"``, so
  deploying this changes no weights. ``shadow`` records what *would* have been
  excluded without excluding it, and only ``enforce`` withholds emissions. An
  unreadable or malformed revision falls back to the same default: the
  conservative direction here is to keep paying miners, not to withhold from
  them on a parsing error.
* **Never retroactive.** A terminal clear takes effect from the next emission
  window (:func:`window_start`); nothing in this module can back-pay a held
  period or claw back a settled one. There is no mechanism for either, by
  design.

Scores stay visible throughout. This gates *emission eligibility* only: the
board keeps publishing the score and its rank, marked provisional, with the
human-readable reason it is not yet earning
(:attr:`AgentEmissionEligibility.reason`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EligibilityEnforcement = Literal["off", "shadow", "enforce"]
"""``off`` evaluates nothing, ``shadow`` records exclusions without applying
them, ``enforce`` withholds emissions. Same ladder as ``CourtMode``."""

DEFAULT_ENFORCEMENT: EligibilityEnforcement = "off"
"""Shipping posture. Merging this cannot move emissions; an operator must
write a revision to change that, and the revision log records who did."""

MIN_ACTIVATION_WINDOW_SECONDS = 60
MAX_ACTIVATION_WINDOW_SECONDS = 86_400
DEFAULT_ACTIVATION_WINDOW_SECONDS = 3_600
"""One hour: the validator epoch length, so a clear lands at an epoch boundary
rather than mid-fold. See :func:`window_start` for why the boundary matters."""

EligibilityState = Literal[
    "eligible",
    "unresolved_review",
    "review_inconclusive",
    "review_escalated",
    "review_infrastructure_failed",
    "review_missing",
    "review_rejected",
    "awaiting_next_window",
]
"""Why one exact artifact is or is not earning.

Every value except ``eligible`` is a withheld state under ``enforce``. The names
are the review vocabulary this repository already uses:
``review_inconclusive`` is ``deferred_source_review.INCONCLUSIVE_REASON_CODE``,
``review_infrastructure_failed`` is
``screening_infra_retry.INFRA_AUTO_RETRY_REASON_CODES``, ``review_escalated`` is
the copy-hold court's ``escalate`` verdict or an ``anomalous_score`` hold, and
``unresolved_review`` is a pending ``ath_reviews`` row of any kind.
"""

WITHHELD_STATES: frozenset[str] = frozenset(
    {
        "unresolved_review",
        "review_inconclusive",
        "review_escalated",
        "review_infrastructure_failed",
        "review_missing",
        "review_rejected",
        "awaiting_next_window",
    }
)

STATE_REASONS: dict[str, str] = {
    "eligible": (
        "Review is terminal for this exact artifact; the score is earning emissions."
    ),
    "unresolved_review": (
        "Source review for this exact artifact is still open. The score and its "
        "rank stand; emissions wait for a terminal review decision."
    ),
    "review_inconclusive": (
        "Automated source review finished without a finding (it ran out of "
        "budget). This is not an accusation; emissions wait for an operator "
        "decision on this exact artifact."
    ),
    "review_escalated": (
        "Source review for this exact artifact was escalated to operator "
        "review. The score and its rank stand; emissions wait for the ruling."
    ),
    "review_infrastructure_failed": (
        "Ditto's own build or review infrastructure failed before this artifact "
        "could be reviewed. This is not a finding against the submission and is "
        "retried automatically; emissions wait for the review to complete."
    ),
    "review_missing": (
        "No completed source review is on record for this exact artifact; "
        "emissions wait for one."
    ),
    "review_rejected": (
        "Source review for this exact artifact ended in a rejection. The score "
        "and the full review history stay published; the artifact does not earn "
        "emissions. A new submission starts a new review."
    ),
    "awaiting_next_window": (
        "Source review cleared this exact artifact. Emissions start at the next "
        "emission window -- a clear is never applied backwards, so no reward is "
        "granted for the period under review."
    ),
}
"""Miner-facing text, one per state. Public surfaces publish these verbatim so
the board, the submission page and Backroom cannot disagree about *why* a score
is not yet earning. No source, prompt, or reviewer output appears here."""


class EmissionEligibilitySettings(BaseModel):
    """Complete subnet-global reward-eligibility posture, stored per revision."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    enforcement: EligibilityEnforcement = DEFAULT_ENFORCEMENT
    """``off`` (default) evaluates nothing and serves a byte-identical ledger.

    ``shadow`` evaluates every ledger row and appends one durable record per
    artifact per window for everything it *would* have excluded, while the
    ledger keeps paying exactly as before -- the rehearsal an operator runs
    before flipping to ``enforce``. ``enforce`` drops those rows from the
    ledger the validator folds, so they earn nothing.
    """

    require_terminal_review: bool = True
    """Withhold while a review for this exact artifact is still open
    (``ath_reviews.status = 'pending'``, including a stranded hold whose
    ``agents.status`` has moved back to ``scored``)."""

    exclude_inconclusive: bool = True
    """Withhold on an inconclusive automated review. A budget outcome is not a
    finding (#2077), which is why its published reason says so -- but an
    unfinished mandatory verification is exactly what #2051 says must not earn.
    """

    exclude_infrastructure_failed: bool = True
    """Withhold while Ditto's own infrastructure is why the review did not
    finish. Never a miner violation (#2051): the state is published as a Ditto
    fault, the retry is automatic, and nothing is rejected."""

    exclude_escalated: bool = True
    """Withhold on a hold the copy-hold court escalated, or an
    ``anomalous_score`` escalation, until an operator rules."""

    require_completed_review: bool = False
    """Off by default, and deliberately so.

    With it on, an artifact with no completed review on record earns nothing.
    That is the strictest reading of the contract, but it would also withhold
    from every submission whose review history predates the record-keeping this
    gate reads, so it stays an explicit operator choice rather than something a
    deploy turns on.
    """

    clearance_activation: Literal["next_window"] = "next_window"
    """The only supported activation rule, named so the wire states it.

    A terminal clear takes effect at the next window boundary, never inside the
    window it landed in. Two reasons: a mid-window change splits the fleet into
    validators that read before it and after it (the problem
    :mod:`ditto.api_server.ledger_pin` exists to remove), and a rule that
    reached backwards would be a retroactive grant, which #2041 forbids.
    """

    activation_window_seconds: Annotated[
        int,
        Field(
            ge=MIN_ACTIVATION_WINDOW_SECONDS,
            le=MAX_ACTIVATION_WINDOW_SECONDS,
        ),
    ] = DEFAULT_ACTIVATION_WINDOW_SECONDS
    """Length of the tumbling UTC window :func:`window_start` quantizes to."""


DEFAULT_SETTINGS = EmissionEligibilitySettings()
"""The posture a platform with no revision stored serves: enforcement off.

Identical to the behaviour before this gate existed, so adding the surface
changes no weights on deploy and a malformed revision degrades to *paying*.
"""


def window_start(now: datetime, *, window_seconds: int) -> datetime:
    """Opening instant of the emission window containing ``now``.

    A tumbling UTC window rather than a chain read, for the same reason
    ``burn_share`` is a resolved scalar rather than a schedule: every process
    that evaluates eligibility -- the ledger materializer, the epoch pin
    builder, the public projection and the Backroom read -- must agree without
    coordinating, and flooring a UTC timestamp needs no agreement at all.

    A terminal clear at ``t`` is honoured from the first window that opens
    strictly after ``t``, so the answer inside any one window is fixed: the pool
    a validator folds cannot change under it because an operator cleared a hold
    thirty seconds ago.
    """
    if window_seconds < MIN_ACTIVATION_WINDOW_SECONDS:
        window_seconds = MIN_ACTIVATION_WINDOW_SECONDS
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((now.astimezone(UTC) - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - (elapsed % window_seconds))


def next_window_start(now: datetime, *, window_seconds: int) -> datetime:
    """Opening instant of the window after the one containing ``now``."""
    return window_start(now, window_seconds=window_seconds) + timedelta(
        seconds=max(window_seconds, MIN_ACTIVATION_WINDOW_SECONDS)
    )


class AgentEmissionEligibility(BaseModel):
    """The shared eligibility record, bound to one exact artifact.

    One record type is read by the validator ledger, the public board, the
    submission page and Backroom, so a miner asking "why am I not earning" and
    an operator asking "why did the fold skip that row" are answered from the
    same evaluation rather than two re-derivations that can disagree
    (#2041 acceptance: *validator folds and public projections consume the same
    eligibility record*).

    ``agent_id`` + ``artifact_sha256`` + ``bench_version`` + ``policy_revision``
    + ``policy_checksum`` is the binding the issue asks for: a different upload,
    a different benchmark contract, or a different posture is a different record.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    artifact_sha256: str
    bench_version: int | None = None
    policy_revision: int
    policy_checksum: str
    enforcement: EligibilityEnforcement
    state: EligibilityState
    reason: str
    """Miner-facing text from :data:`STATE_REASONS`; never reviewer output."""
    reward_eligible: bool
    """Whether this artifact earns emissions under the *current* posture.

    Under ``off`` and ``shadow`` this is ``True`` for every scored row, because
    those postures do not withhold. Read ``posture_satisfied`` for the verdict
    the gate would reach if it were enforcing.
    """
    posture_satisfied: bool
    """Whether the required review posture is met, independent of enforcement.

    This is what the shadow rehearsal publishes: ``False`` here with
    ``reward_eligible`` ``True`` is exactly a row that ``enforce`` would drop.
    """
    window_start: datetime
    """Opening instant of the window this verdict was evaluated for."""
    activates_at: datetime | None = None
    """When a cleared artifact starts earning, for ``awaiting_next_window``."""
    review_status: str | None = None
    review_resolution: str | None = None
    review_kind: str | None = None
    review_resolved_at: datetime | None = None
    screening_reason_code: str | None = None
    """Existing vocabulary, echoed unchanged so an operator can join back to
    ``ath_reviews`` / ``screening_attempts`` without a translation table."""


class EmissionEligibilitySettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: EmissionEligibilitySettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveEmissionEligibilitySettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: EmissionEligibilitySettings
    checksum: str
    source: Literal["revision", "default"]
    max_age_seconds: float
    """Resolver TTL: the longest a validator's next ledger read can still be
    answered from the previous revision."""

    default: EmissionEligibilitySettings = DEFAULT_SETTINGS
    """The fallback posture, served so the console can state what a malformed or
    missing revision degrades to instead of guessing."""

    current_window_start: datetime
    next_window_start: datetime
    """The boundary a clear recorded now would take effect at. Published because
    "when does this miner start earning" is otherwise an operator calculation.
    """

    live_validator_count: int | None = None
    """Validators heartbeating recently enough to be folding weights. A posture
    change reaches the chain only through them; ``None`` when unreadable."""

    shadow_excluded_count: int | None = None
    """Distinct artifacts the shadow rehearsal has recorded as *would have been
    excluded* in the current window. The number an operator checks before
    flipping to ``enforce``; ``None`` when unreadable."""


class AdminEmissionEligibilitySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: EmissionEligibilitySettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminEmissionEligibilityShadowRecord(BaseModel):
    """One append-only rehearsal row: what ``enforce`` would have withheld."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    artifact_sha256: str
    bench_version: int | None
    state: EligibilityState
    reason: str
    policy_revision: int
    policy_checksum: str
    enforcement: EligibilityEnforcement
    window_start: datetime
    created_at: datetime


class AdminEmissionEligibilitySettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: EmissionEligibilitySettingsRevision | None
    history: list[EmissionEligibilitySettingsRevision]
    default: EmissionEligibilitySettings
    effective: EffectiveEmissionEligibilitySettings
    confirmation_phrase: str
    recent_shadow_records: list[AdminEmissionEligibilityShadowRecord]
    """Newest rehearsal rows, so the posture page and the evidence for changing
    it are one read."""


class AdminAgentEmissionEligibilityResponse(BaseModel):
    """Per-agent eligibility read for Backroom and the operator console."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    eligibility: AgentEmissionEligibility
    in_ledger: bool
    """Whether the agent is in the pool the validator currently folds. ``False``
    with a terminal review means something other than this gate is holding it
    (``agents.status``, the ranking floor, or a rollout version pin)."""
    effective: EffectiveEmissionEligibilitySettings
    shadow_records: list[AdminEmissionEligibilityShadowRecord]


def eligibility_checksum(settings: EmissionEligibilitySettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()

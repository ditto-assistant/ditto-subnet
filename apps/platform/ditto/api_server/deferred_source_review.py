"""Mechanical-first admission and post-score deep source-review decisions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError

from ditto.api_models.public import (
    PublicDeferredReviewTrigger,
    PublicReviewConclusion,
)
from ditto.api_models.queue_policy_settings import DeferredSourceReviewSettings
from ditto_screening_protocol.models import ScreenReviewAudit
from ditto_screening_protocol.review_ledger import concern_threshold_reached

if TYPE_CHECKING:
    from ditto.db.queries.scores import LedgerRow

DEFERRED_REVIEW_KIND = "deferred_source_review"
DEFERRED_REVIEW_REASON = "Score qualified this submission for deferred source review"
DEFERRED_MECHANICAL_REASON = "deferred-mechanical-admission"
INCONCLUSIVE_REASON_CODE = "source-review-inconclusive"
# Public text written with INCONCLUSIVE_REASON_CODE. Clients classify a hold by
# ``review_conclusion`` (see ``public_review_conclusion``), never by this text.
SOURCE_REVIEW_INCONCLUSIVE_PUBLIC_REASON = (
    "Bounded source review was inconclusive; held for review"
)
TOP_FIVE_SIZE = 5
# A second, stronger deep review for every top-five entrant, including ones
# that already passed the full pre-score screen. It shares the deferred hold
# lifecycle (``review_kind`` stays ``deferred_source_review``) so the screener
# re-claim, auto-clear on a clean pass, and operator adjudication are one path.
INTEGRITY_DOUBLE_CHECK_AUDIT_KIND = "integrity_double_check"
INTEGRITY_DOUBLE_CHECK_REASON = (
    "Top-five rank qualified this submission for an integrity double-check"
)
INTEGRITY_DOUBLE_CHECK_ALGORITHM = "integrity-double-check-v1"
INTEGRITY_DOUBLE_CHECK_ACTOR = "platform:integrity-double-check"
# ``algorithm_provenance["trigger"]`` on a double-check hold. The review kind
# stays ``deferred_source_review``; this marker selects the reviewer posture.
INTEGRITY_DOUBLE_CHECK_TRIGGER = "integrity_double_check"


@dataclass(frozen=True)
class DeferredReviewDecision:
    triggered: bool
    triggers: tuple[
        Literal["top_five", "composite_anomaly", "tool_anomaly", "memory_anomaly"], ...
    ]
    rank: int | None
    evidence: dict[str, object]


def _median_mad(values: Sequence[float]) -> tuple[float, float]:
    center = float(median(values))
    return center, float(median(abs(value - center) for value in values))


def evaluate_deferred_review(
    *,
    agent_id: UUID,
    ledger: Sequence[LedgerRow],
    settings: DeferredSourceReviewSettings,
) -> DeferredReviewDecision:
    """Classify one finalized row against the same-version canonical ledger.

    The ledger already applies the platform's one-emission-owner and quorum
    rules. Ranking it again here would create a second definition of top five,
    so this function consumes its canonical order directly.
    """
    candidate = next((row for row in ledger if row.agent_id == agent_id), None)
    if candidate is None or not candidate.eligible:
        return DeferredReviewDecision(False, (), None, {"eligible": False})

    rank = next(
        index for index, row in enumerate(ledger, start=1) if row.agent_id == agent_id
    )
    peers = [row for row in ledger if row.agent_id != agent_id and row.eligible]
    triggers: list[
        Literal["top_five", "composite_anomaly", "tool_anomaly", "memory_anomaly"]
    ] = []
    if rank <= TOP_FIVE_SIZE:
        triggers.append("top_five")

    metrics: dict[str, object] = {
        "eligible": True,
        "rank": rank,
        "cohort_size": len(ledger),
        "peer_count": len(peers),
        "candidate": {
            "composite": candidate.composite,
            "tool_mean": candidate.tool_mean,
            "memory_mean": candidate.memory_mean,
        },
    }
    if len(peers) >= settings.min_cohort_size:
        thresholds: dict[str, dict[str, float]] = {}
        for field, trigger, multiplier, floor in (
            (
                "composite",
                "composite_anomaly",
                settings.composite_mad_multiplier,
                settings.min_composite_delta,
            ),
            (
                "tool_mean",
                "tool_anomaly",
                settings.axis_mad_multiplier,
                settings.min_axis_delta,
            ),
            (
                "memory_mean",
                "memory_anomaly",
                settings.axis_mad_multiplier,
                settings.min_axis_delta,
            ),
        ):
            values = [float(getattr(row, field)) for row in peers]
            center, mad = _median_mad(values)
            threshold = min(1.0, center + max(floor, multiplier * mad))
            thresholds[field] = {
                "median": center,
                "mad": mad,
                "threshold": threshold,
            }
            if float(getattr(candidate, field)) > threshold:
                triggers.append(trigger)  # type: ignore[arg-type]
        metrics["thresholds"] = thresholds
    else:
        metrics["thresholds"] = None
        metrics["anomaly_unavailable"] = "cohort_too_small"

    metrics["triggers"] = list(triggers)
    metrics["mode"] = settings.mode
    return DeferredReviewDecision(bool(triggers), tuple(triggers), rank, metrics)


def evaluate_integrity_double_check(
    *,
    agent_id: UUID,
    ledger: Sequence[LedgerRow],
    held_composites: Sequence[float],
) -> DeferredReviewDecision:
    """Qualify one ledger row for the top-five integrity double-check.

    ``held_composites`` are the composites of submissions already held for a
    post-score deep review at this benchmark version. The canonical ledger
    drops held agents, so without them every hold would promote the next row
    into the top five and the holds would cascade down the whole board while
    reviews are running. A held row at or above the candidate still occupies
    its slot; ties count against the candidate so the cap stays conservative.
    """
    candidate = next((row for row in ledger if row.agent_id == agent_id), None)
    if candidate is None or not candidate.eligible:
        return DeferredReviewDecision(False, (), None, {"eligible": False})
    ledger_rank = next(
        index for index, row in enumerate(ledger, start=1) if row.agent_id == agent_id
    )
    held_above = sum(
        1 for composite in held_composites if composite >= candidate.composite
    )
    rank = ledger_rank + held_above
    triggered = rank <= TOP_FIVE_SIZE
    evidence: dict[str, object] = {
        "eligible": True,
        "rank": rank,
        "ledger_rank": ledger_rank,
        "held_above": held_above,
        "cohort_size": len(ledger),
        "candidate": {
            "composite": candidate.composite,
            "tool_mean": candidate.tool_mean,
            "memory_mean": candidate.memory_mean,
        },
        "triggers": ["top_five"] if triggered else [],
        "integrity_double_check": True,
    }
    return DeferredReviewDecision(
        triggered, ("top_five",) if triggered else (), rank, evidence
    )


# ── Public projection (#562) ──────────────────────────────────────────────────
#
# The public dashboard must say WHY a submission sits in the deferred branch and
# WHAT the automated stage concluded, without exposing ranks, thresholds,
# findings, notes, audits, or raw reason codes. Both projections below are
# closed enums derived from reason codes; nothing else from the evidence leaves.

_ANOMALY_TRIGGERS = frozenset({"composite_anomaly", "tool_anomaly", "memory_anomaly"})


def public_deferred_review_triggers(
    evidence: object,
) -> list[PublicDeferredReviewTrigger]:
    """Coarse public trigger kinds for one deferred-review evidence snapshot.

    ``evidence`` is ``AthReview.original_evidence``. Unknown or malformed
    trigger values are dropped rather than echoed.
    """
    deferred = evidence.get("deferred_review") if isinstance(evidence, dict) else None
    raw = deferred.get("triggers") if isinstance(deferred, dict) else None
    if not isinstance(raw, list):
        return []
    kinds = {str(trigger) for trigger in raw}
    triggers: list[PublicDeferredReviewTrigger] = []
    if "top_five" in kinds:
        triggers.append("top_five")
    if kinds & _ANOMALY_TRIGGERS:
        triggers.append("anomaly")
    return triggers


# Exact budget-exhaustion codes a screener can report today: the source
# reviewer's ``source-review-*`` codes (workers/screener source_review.py), the
# L2 reviewer's ``l2-*`` codes (l2_review.py ``_error_code("l2", ...)``), and the
# bare lease code from gate.py/worker.py. An exact set, not a suffix match, so a
# future ``*-budget-exhausted`` code that can carry a finding is never softened.
BUDGET_EXHAUSTED_REASON_CODES = frozenset(
    {
        "source-review-read-budget-exhausted",
        "source-review-step-budget-exhausted",
        "source-review-lease-budget-exhausted",
        "l2-lease-budget-exhausted",
        "l2-model-budget-exhausted",
        "lease-budget-exhausted",
    }
)
# V13 L2 reviewer codes that carry no verdict: the model's bounded
# "inconclusive" disposition and its trajectory budgets (the no-verdict codes
# ``screening_quarantines_review_audit_reason_check`` admits alongside
# ``source-review-inconclusive``), plus the signed-runtime preflight hold
# (``l2-runtime-evidence-unavailable``: lease unavailable or review disabled),
# which a strict V13 INCONCLUSIVE verdict can carry as its reason code. Only a
# proving audit softens any of them, and a preflight audit never proves a
# review ran, so the preflight code can reach ``not_completed`` at most.
L2_NO_VERDICT_REASON_CODES = frozenset(
    {
        "l2-runtime-evidence-unavailable",
        "l2-model-inconclusive",
        "l2-model-total-budget",
        "l2-model-tool-budget",
        "l2-model-step-budget",
    }
)
# A deep attempt that stopped before any verdict: provider or platform outage,
# or the runtime health re-check interrupted on a different screener host
# (endpoints/screener.py keeps the hold and parks it for a manual retry).
_INTERRUPTED_OUTCOME = "retryable_infra"
_INTERRUPTED_REJECT = ("deterministic_reject", "health-contract")


def is_no_finding_reason_code(reason_code: str | None) -> bool:
    """True for screening reason codes that carry no verdict and no finding.

    An inconclusive review and a known exhausted review budget stopped before
    reaching a verdict. This gates only which codes may be softened; whether a
    model review actually ran is decided from the recorded review audit. Every
    other code, including unknown ones, is treated as an adverse signal so a
    new finding code can never be softened by omission.
    """
    return reason_code is not None and (
        reason_code == INCONCLUSIVE_REASON_CODE
        or reason_code in BUDGET_EXHAUSTED_REASON_CODES
        or reason_code in L2_NO_VERDICT_REASON_CODES
    )


# Audit reason codes that prove a model review ran AND stopped on a budget it
# was given: the L1 reviewer's ``SourceReviewBudgetExhausted.audit()`` and the
# L2 trajectory budgets (l2_review.py ``L2TrajectoryError`` ->
# ``budget_stop_reason`` aggregate/tool/step). Exact, like the reason-code set.
BUDGET_AUDIT_REASON_CODES = frozenset(
    {
        "source-review-read-budget-exhausted",
        "source-review-step-budget-exhausted",
        "source-review-lease-budget-exhausted",
        "l2-model-total-budget",
        "l2-model-tool-budget",
        "l2-model-step-budget",
    }
)
_BUDGET_STOP_REASONS = frozenset({"step", "tool", "aggregate", "token", "cost", "time"})


def _carries_finding(finding_digest: object, finding: object) -> bool:
    return finding_digest is not None or finding is not None


def _recorded_review_outcome(
    raw_audit: object,
) -> Literal["not_completed", "no_finding", "budget_exhausted"]:
    """Classify a no-verdict hold by the review audit actually recorded with it.

    Only a well-formed ``ScreenReviewAudit`` whose counters show model steps
    proves a model review ran to a budget or a bounded disposition. Anything
    else is ``not_completed``, which claims only what the record shows -- that
    no review completed with a recorded conclusion -- because the record cannot
    say how far review got:

    - no audit (a stored JSON ``null`` reads back as ``None`` too): legacy rows
      from before the audit field, or the V13 L2 path that fails after L1 has
      already read the source (scorer-capabilities fetch failure);
    - a malformed audit;
    - the V13 signed-runtime preflight audit (``final_stage == "preflight"`` /
      a ``cause_detail``: lease unavailable or review disabled), which proves
      only that the L2 model stage never started;
    - a well-formed audit with zero model steps.
    """
    if not isinstance(raw_audit, dict):
        return "not_completed"
    try:
        audit = ScreenReviewAudit.model_validate(raw_audit)
    except ValidationError:
        return "not_completed"
    if audit.final_stage == "preflight" or audit.cause_detail is not None:
        return "not_completed"
    if audit.steps_used == 0 and not audit.model_steps_observed:
        return "not_completed"
    if (
        audit.reason_code in BUDGET_AUDIT_REASON_CODES
        or audit.budget_stop_reason in _BUDGET_STOP_REASONS
    ):
        return "budget_exhausted"
    return "no_finding"


def _no_verdict_conclusion(
    raw_audit: object, raw_notes: object, *, concern_hold_count: int
) -> PublicReviewConclusion:
    """The recorded audit's outcome, unless the hold is concern-driven.

    A budget-terminated review holds either on thin coverage or because its
    recorded concerns reached ``concern_hold_count`` (the worker's
    ``ledger_disposition``). The second hold is caused by the concerns, not the
    budget, so it reads ``adverse_signal``. The threshold rule is the one the
    worker applies, shared through ``ditto_screening_protocol.review_ledger``,
    evaluated on the notes ledger recorded with the result against the review
    settings pinned on that attempt. A budget hold without a recorded notes
    ledger is ``adverse_signal``: nothing on record shows it was thin coverage.
    """
    outcome = _recorded_review_outcome(raw_audit)
    if outcome != "budget_exhausted":
        return outcome
    if not isinstance(raw_notes, list):
        # Fail closed: without the recorded ledger nothing shows the hold was
        # thin coverage rather than concern-driven (legacy quarantines, or any
        # path that did not retain ``review_notes``). The operator-facing
        # ``evidence`` trail is never parsed back into notes: it is a lossy,
        # reshaped copy and could only soften the conclusion.
        return "adverse_signal"
    notes = [note for note in raw_notes if isinstance(note, dict)]
    if concern_threshold_reached(notes, concern_hold_count=concern_hold_count):
        return "adverse_signal"
    return outcome


def public_review_conclusion(
    *,
    deferred_review_active: bool,
    deferred_evidence: object,
    deferred_concern_hold_count: int,
    quarantined: bool,
    screening_reason_code: str | None,
    quarantine_finding_digest: str | None,
    quarantine_finding: object,
    quarantine_review_audit: object,
    quarantine_review_notes: object,
    quarantine_concern_hold_count: int,
) -> PublicReviewConclusion | None:
    """What the automated review concluded for a held submission.

    Precedence, identical on both paths:

    1. a recorded finding (digest or payload) is always ``adverse_signal``;
    2. an interrupted deep attempt has no conclusion yet (``pending``);
    3. a reason code outside the known no-verdict set, including unknown
       codes, is ``adverse_signal``;
    4. otherwise the recorded review audit decides: ``budget_exhausted`` or
       ``no_finding`` only when it proves a model review ran, and
       ``not_completed`` when there is no such proof;
    5. a ``budget_exhausted`` hold whose recorded substantiated concerns reach
       the attempt's ``concern_hold_count`` -- or that has no recorded notes
       ledger at all -- is ``adverse_signal``.

    For an active deferred review the post-score deep attempt's result, its
    ``review_audit`` and ``review_notes`` decide, and the review is ``pending``
    until one is recorded. For a pre-score quarantine the agent's screening
    reason code and the active quarantine's finding, ``review_audit`` and
    ``review_notes`` decide. Every other hold (copy review, operator hold) has
    no automated-review conclusion.
    """
    result = (
        deferred_evidence.get("deep_review_result")
        if deferred_review_active and isinstance(deferred_evidence, dict)
        else None
    )
    if isinstance(result, dict):
        if _carries_finding(result.get("finding_digest"), result.get("finding")):
            return "adverse_signal"
        outcome = result.get("outcome")
        code = result.get("reason_code")
        if outcome == _INTERRUPTED_OUTCOME or (outcome, code) == _INTERRUPTED_REJECT:
            return "pending"
        if not (isinstance(code, str) and is_no_finding_reason_code(code)):
            return "adverse_signal"
        return _no_verdict_conclusion(
            result.get("review_audit"),
            result.get("review_notes"),
            concern_hold_count=deferred_concern_hold_count,
        )
    if quarantined:
        if _carries_finding(quarantine_finding_digest, quarantine_finding):
            return "adverse_signal"
        if screening_reason_code is not None:
            if not is_no_finding_reason_code(screening_reason_code):
                return "adverse_signal"
            return _no_verdict_conclusion(
                quarantine_review_audit,
                quarantine_review_notes,
                concern_hold_count=quarantine_concern_hold_count,
            )
    return "pending" if deferred_review_active else None


def deep_review_attempt_id(deferred_evidence: object) -> UUID | None:
    """The attempt behind an active deferred review's recorded deep result."""
    result = (
        deferred_evidence.get("deep_review_result")
        if isinstance(deferred_evidence, dict)
        else None
    )
    raw = result.get("attempt_id") if isinstance(result, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
